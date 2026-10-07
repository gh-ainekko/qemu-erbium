/* SPDX-License-Identifier: GPL-2.0 */
/*
 * ARM Linux acceptance client for direct hardware wiring option 1.
 * Build: aarch64-linux-gnu-gcc -O2 -static -Wall -Wextra -Werror \
 *        -o build/erbium-uart-test linux/tools/erbium-uart-test.c
 *
 * ttyAMA1 is the serial peer; ttyAMA0/stdin/stdout are NEVER a data transport.
 * Open/configure before fork+exec erbctl load --verify --start. Keep the same
 * TTY open through repeated loads. No input flush, banner search/resync, or
 * mailbox console shortcut: stale/extra bytes are failures.
 * Handled SIGINT/SIGTERM/SIGHUP/SIGQUIT restore termios; SIGKILL/power loss
 * cannot be caught. Do not run a getty or another already-open peer client.
 *
 * This tests backpressured emulator byte streams, not silicon UART timing.
 * B115200 is the Linux-side setting; firmware divider 58 is provisional.
 * Match actual clocks/dividers, voltage and GPIO9/10 wiring on hardware; see
 * docs/uart-host-connectivity.md. No kernel console on UART1 is required or
 * appropriate, and the kernel must expose the independent PL011 UART1.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

static const unsigned char banner[] = "ERBIUM-UART-SMOKE-v1\r\n";
static volatile sig_atomic_t interrupted;
static int tty = -1;
static bool saved_valid, exclusive;
static struct termios saved;
static pid_t loader = -1;
static const char *phase = "setup";
static const char *tty_path = "/dev/ttyAMA1";
static const char *elf = "/firmware/uart-smoke.elf";
static const char *erbctl = "erbctl";
static const char *device = "/dev/erbium0", *mtd = "/dev/mtd0";
static int timeout_ms = 30000, rounds = 2;

static void on_signal(int sig) { interrupted = sig; }

static int64_t now_ms(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) < 0) {
        perror("UART test: clock_gettime");
        exit(1); /* atexit still restores termios */
    }
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int fail(const char *message)
{
    fprintf(stderr, "FAIL: UART test (%s): %s\n", phase, message);
    return -1;
}

static int syserr(const char *message)
{
    fprintf(stderr, "FAIL: UART test (%s): %s: %s\n",
            phase, message, strerror(errno));
    return -1;
}

static void stop_loader(void)
{
    if (loader <= 0)
        return;
    kill(loader, SIGTERM);
    /* Give erbctl time to run its hold-on-cancellation cleanup. */
    for (int i = 0; i < 50; ++i) {
        pid_t p = waitpid(loader, NULL, WNOHANG);
        if (p == loader || (p < 0 && errno == ECHILD)) {
            loader = -1;
            return;
        }
        struct timespec delay = { .tv_nsec = 10000000 };
        nanosleep(&delay, NULL);
    }
    kill(loader, SIGKILL);
    while (waitpid(loader, NULL, 0) < 0 && errno == EINTR) {}
    loader = -1;
}

static int restore_tty(void)
{
    int result = 0;
    stop_loader();
    if (tty >= 0) {
        if (saved_valid) {
            int r;
            do { r = tcsetattr(tty, TCSANOW, &saved); } while (r < 0 && errno == EINTR);
            if (r < 0)
                result = syserr("restore original termios");
            saved_valid = false;
        }
        if (exclusive && ioctl(tty, TIOCNXCL) < 0)
            result = syserr("release TTY exclusivity");
        exclusive = false;
        close(tty);
        tty = -1;
    }
    return result;
}

static void cleanup(void) { (void)restore_tty(); }

static int remaining(int64_t deadline)
{
    if (interrupted)
        return fail("interrupted; restoring TTY");
    int64_t ms = deadline - now_ms();
    if (ms <= 0)
        return fail("wall-clock timeout");
    return (int)ms;
}

static int wait_tty(short events, int64_t deadline)
{
    for (;;) {
        int ms = remaining(deadline);
        if (ms < 0)
            return -1;
        struct pollfd p = { .fd = tty, .events = events };
        int n = poll(&p, 1, ms);
        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0)
            return syserr("poll TTY");
        if (n == 0)
            return fail("wall-clock timeout");
        if (p.revents & (POLLERR | POLLHUP | POLLNVAL))
            return fail("TTY disconnected/error");
        if (p.revents & events)
            return p.revents;
    }
}

/* No discarded input: even a single unrequested byte is a test failure. */
static int assert_quiet(int duration_ms)
{
    int64_t deadline = now_ms() + duration_ms;
    for (;;) {
        if (interrupted)
            return fail("interrupted; restoring TTY");
        int64_t ms = deadline - now_ms();
        if (ms <= 0)
            return 0;
        struct pollfd p = { .fd = tty, .events = POLLIN };
        int n = poll(&p, 1, (int)ms);
        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0)
            return syserr("poll quiet TTY");
        if (n == 0)
            return 0;
        if (p.revents & (POLLERR | POLLHUP | POLLNVAL))
            return fail("TTY disconnected/error while checking stale bytes");
        if (p.revents & POLLIN) {
            unsigned char byte;
            ssize_t r = read(tty, &byte, 1);
            if (r < 0 && (errno == EINTR || errno == EAGAIN))
                continue;
            if (r < 0)
                return syserr("read quiet TTY");
            if (r == 0)
                continue; /* Raw VMIN=0 may return no data. poll bounds retry. */
            fprintf(stderr, "FAIL: UART test (%s): stale/extra byte 0x%02x\n",
                    phase, byte);
            return -1;
        }
    }
}

static int open_peer(void)
{
    tty = open(tty_path, O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC);
    if (tty < 0)
        return syserr("open serial peer");
    struct stat peer, console, stream;
    if (fstat(tty, &peer) < 0)
        return syserr("stat serial peer");
    if (!S_ISCHR(peer.st_mode))
        return fail("serial peer is not a character device");
    const char *console_paths[] = { "/dev/ttyAMA0", "/dev/console", "/dev/tty" };
    for (size_t i = 0; i < sizeof(console_paths) / sizeof(console_paths[0]); ++i) {
        if (stat(console_paths[i], &console) == 0 && peer.st_rdev == console.st_rdev)
            return fail("refusing Linux console0/console alias as serial peer");
    }
    for (int fd = 0; fd <= 2; ++fd) {
        if (isatty(fd) && fstat(fd, &stream) == 0 && peer.st_rdev == stream.st_rdev)
            return fail("serial peer must not be stdin/stdout/stderr");
    }
    /* flock serializes cooperating root clients; TIOCEXCL blocks later opens
     * by ordinary processes. Neither can evict an already-open console/getty. */
    if (flock(tty, LOCK_EX | LOCK_NB) < 0)
        return syserr("lock serial peer");
    if (ioctl(tty, TIOCEXCL) < 0)
        return syserr("exclusive serial peer");
    exclusive = true;
    if (tcgetattr(tty, &saved) < 0)
        return syserr("save termios");
    saved_valid = true;
    struct termios raw = saved;
    cfmakeraw(&raw);
    raw.c_cflag &= ~(CSIZE | PARENB | PARODD | CSTOPB | CRTSCTS);
    raw.c_cflag |= CS8 | CLOCAL | CREAD;
    raw.c_cc[VMIN] = 0;
    raw.c_cc[VTIME] = 0;
    if (cfsetispeed(&raw, B115200) < 0 || cfsetospeed(&raw, B115200) < 0 ||
        tcsetattr(tty, TCSANOW, &raw) < 0)
        return syserr("set raw 115200 8N1 (no XON/XOFF)");
    return 0; /* NEVER tcflush: attach before release and preserve the banner. */
}

static int start_firmware(int64_t deadline)
{
    loader = fork();
    if (loader < 0)
        return syserr("fork erbctl");
    if (loader == 0) {
        const int signals[] = { SIGINT, SIGTERM, SIGHUP, SIGQUIT };
        for (size_t i = 0; i < sizeof(signals) / sizeof(signals[0]); ++i)
            signal(signals[i], SIG_DFL);
        /* O_CLOEXEC ensures erbctl cannot inherit/consume serial-peer input. */
        execlp(erbctl, erbctl, "-d", device, "load", elf, "--mtd", mtd,
               "--verify", "--start", (char *)NULL);
        fprintf(stderr, "FAIL: UART test: exec %s: %s\n", erbctl, strerror(errno));
        _exit(127);
    }
    for (;;) {
        if (remaining(deadline) < 0)
            return -1;
        int status;
        pid_t p = waitpid(loader, &status, WNOHANG);
        if (p == loader) {
            loader = -1;
            if (!WIFEXITED(status) || WEXITSTATUS(status) != 0)
                return fail("erbctl load --verify --start failed");
            return 0;
        }
        if (p < 0 && errno != EINTR)
            return syserr("waitpid erbctl");
        struct timespec delay = { .tv_nsec = 10000000 };
        nanosleep(&delay, NULL);
    }
}

static int read_banner(int64_t deadline)
{
    size_t used = 0;
    while (used < sizeof(banner) - 1) {
        unsigned char buf[sizeof(banner) - 1];
        if (wait_tty(POLLIN, deadline) < 0)
            return -1;
        ssize_t n = read(tty, buf, sizeof(banner) - 1 - used);
        if (n < 0 && (errno == EINTR || errno == EAGAIN))
            continue;
        if (n < 0)
            return syserr("read firmware banner");
        for (ssize_t i = 0; i < n; ++i) {
            if (buf[i] != banner[used + (size_t)i]) {
                fprintf(stderr, "FAIL: UART test: banner offset %zu: expected "
                        "0x%02x got 0x%02x (stale bytes/wrong endpoint?)\n",
                        used + (size_t)i, banner[used + (size_t)i], buf[i]);
                return -1;
            }
        }
        used += (size_t)n;
    }
    return 0;
}

/* Poll/read/write BOTH directions, never write-all then read-all: finite FIFOs
 * and a slow output reader must not deadlock a burst longer than 16 bytes. */
static int echo_case(const unsigned char *data, size_t length)
{
    size_t sent = 0, received = 0;
    int64_t deadline = now_ms() + timeout_ms;
    while (received < length) {
        short events = POLLIN | (sent < length ? POLLOUT : 0);
        int ready = wait_tty(events, deadline);
        if (ready < 0)
            return -1;
        if (ready & POLLIN) {
            unsigned char buf[256];
            ssize_t n = read(tty, buf, sizeof(buf));
            if (n < 0 && errno != EINTR && errno != EAGAIN)
                return syserr("read echo");
            if (n > 0) {
                for (ssize_t i = 0; i < n; ++i) {
                    if (received >= sent || received >= length ||
                        buf[i] != data[received]) {
                        fprintf(stderr, "FAIL: UART test (%s): echo mismatch/"
                                "extra byte at %zu, sent=%zu got=0x%02x\n",
                                phase, received, sent, buf[i]);
                        return -1;
                    }
                    ++received;
                }
            }
        }
        if ((ready & POLLOUT) && sent < length) {
            ssize_t n = write(tty, data + sent, length - sent);
            if (n < 0 && errno != EINTR && errno != EAGAIN)
                return syserr("write payload");
            if (n > 0)
                sent += (size_t)n;
        }
    }
    return assert_quiet(100);
}

static int number(const char *arg, int limit)
{
    char *end;
    errno = 0;
    long n = strtol(arg, &end, 10);
    if (errno || !*arg || *end || n <= 0 || n > limit)
        return -1;
    return (int)n;
}

static void usage(const char *name)
{
    fprintf(stderr, "usage: %s [--tty /dev/ttyAMA1] [--elf /firmware/uart-smoke.elf]\n"
            "       [--erbctl erbctl] [--device /dev/erbium0] [--mtd /dev/mtd0]\n"
            "       [--timeout-ms 30000] [--rounds 2 (minimum 2)]\n", name);
}

int main(int argc, char **argv)
{
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--help")) {
            usage(argv[0]);
            return 0;
        }
        if (i + 1 == argc) {
            usage(argv[0]);
            return 2;
        }
        const char *key = argv[i++], *value = argv[i];
        if (!strcmp(key, "--tty")) tty_path = value;
        else if (!strcmp(key, "--elf")) elf = value;
        else if (!strcmp(key, "--erbctl")) erbctl = value;
        else if (!strcmp(key, "--device")) device = value;
        else if (!strcmp(key, "--mtd")) mtd = value;
        else if (!strcmp(key, "--timeout-ms")) timeout_ms = number(value, 600000);
        else if (!strcmp(key, "--rounds")) rounds = number(value, 100);
        else { usage(argv[0]); return 2; }
    }
    if (timeout_ms < 0 || rounds < 2) {
        usage(argv[0]); /* Repeat load is mandatory acceptance, not optional. */
        return 2;
    }
    if (atexit(cleanup) != 0)
        return 1;
    struct sigaction sa = { .sa_handler = on_signal };
    sigemptyset(&sa.sa_mask);
    const int signals[] = { SIGINT, SIGTERM, SIGHUP, SIGQUIT };
    for (size_t i = 0; i < sizeof(signals) / sizeof(signals[0]); ++i) {
        if (sigaction(signals[i], &sa, NULL) < 0)
            return syserr("install cleanup signal handler") < 0;
    }
    int result = 1;
    if (open_peer() < 0)
        goto out;
    for (int round = 0; round < rounds; ++round) {
        fprintf(stderr, "== UART test: load/start %d/%d, same open %s\n",
                round + 1, rounds, tty_path);
        phase = "before release (no stale bytes)";
        if (assert_quiet(200) < 0)
            goto out;
        phase = "load/start and fresh firmware banner";
        int64_t deadline = now_ms() + timeout_ms;
        if (start_firmware(deadline) < 0 || read_banner(deadline) < 0 ||
            assert_quiet(100) < 0)
            goto out;
        static const unsigned char binary[] = { 0, '\r', '\n', 0x11, 0x13, 0xff };
        phase = "NUL/CR/LF/XON/XOFF/FF transparency";
        if (echo_case(binary, sizeof(binary)) < 0)
            goto out;
        unsigned char burst[1024];
        for (size_t i = 0; i < sizeof(burst); ++i)
            burst[i] = (unsigned char)(i + (size_t)round * 37);
        phase = "1024-byte sequential burst, full-duplex polling";
        if (echo_case(burst, sizeof(burst)) < 0)
            goto out;
    }
    result = 0;
out:
    phase = "cleanup";
    if (restore_tty() < 0 || interrupted)
        result = 1;
    if (!result)
        puts("ALL UART TESTS PASSED");
    return result;
}
