// SPDX-License-Identifier: GPL-2.0
/* Direct physical UART wiring, independent of the xSPI loader/control lock.
 * No RX flush: attaching before a separate `erbctl load --start` retains output.
 */
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
#include <termios.h>
#include <time.h>
#include <unistd.h>
#include "erbium-console.h"

#define QUEUE_SIZE 16384
#define QUIET_MS 500
#define DRAIN_MS 3000

struct queue {
	unsigned char bytes[QUEUE_SIZE];
	size_t len;
};

static volatile sig_atomic_t stopped;
static void console_signal(int sig) { stopped = sig; }

static int64_t milliseconds(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int diagnostic(const char *what)
{
	fprintf(stderr, "erbctl: console: %s: %s\n", what, strerror(errno));
	return 1;
}

static int usage(void)
{
	fprintf(stderr, "usage: erbctl console [TTY (default /dev/ttyAMA1)] [--baud N]\n"
		"  Raw 8N1, default 115200 baud; Ctrl-] exits on terminal stdin.\n"
		"  Pipe stdin is binary (no escape); EOF drains until 500 ms quiet,\n"
		"  with a 3 s overall deadline. Attach before load --start if desired.\n");
	return 2;
}

static bool baud_speed(const char *text, speed_t *speed)
{
	static const struct { unsigned long number; speed_t speed; } rates[] = {
		{50, B50}, {75, B75}, {110, B110}, {134, B134}, {150, B150},
		{200, B200}, {300, B300}, {600, B600}, {1200, B1200},
		{1800, B1800}, {2400, B2400}, {4800, B4800}, {9600, B9600},
		{19200, B19200}, {38400, B38400}, {57600, B57600},
		{115200, B115200}, {230400, B230400},
#ifdef B460800
		{460800, B460800},
#endif
#ifdef B500000
		{500000, B500000},
#endif
#ifdef B576000
		{576000, B576000},
#endif
#ifdef B921600
		{921600, B921600},
#endif
#ifdef B1000000
		{1000000, B1000000},
#endif
#ifdef B1500000
		{1500000, B1500000},
#endif
#ifdef B2000000
		{2000000, B2000000},
#endif
#ifdef B2500000
		{2500000, B2500000},
#endif
#ifdef B3000000
		{3000000, B3000000},
#endif
#ifdef B3500000
		{3500000, B3500000},
#endif
#ifdef B4000000
		{4000000, B4000000},
#endif
	};
	char *end;
	unsigned long n;
	if (!*text || strspn(text, "0123456789") != strlen(text))
		return false;
	errno = 0;
	n = strtoul(text, &end, 10);
	if (errno || *end)
		return false;
	for (size_t i = 0; i < sizeof(rates) / sizeof(rates[0]); i++)
		if (n == rates[i].number) {
			*speed = rates[i].speed;
			return true;
		}
	return false;
}

/* Each readiness event does at most one bounded syscall. */
static int write_queue(int fd, struct queue *q)
{
	ssize_t n = write(fd, q->bytes, q->len);
	if (n < 0) {
		if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)
			return 0;
		return -1;
	}
	if (!n) { errno = EIO; return -1; }
	q->len -= (size_t)n;
	memmove(q->bytes, q->bytes + n, q->len);
	return 0;
}

int erbctl_console(int argc, char **argv)
{
	const char *tty = "/dev/ttyAMA1", *baud = "115200";
	bool have_tty = false, have_baud = false;
	speed_t speed;
	int uart = -1, result = 1, flags[2] = {-1, -1}, handlers = 0;
	const int streams[2] = {STDIN_FILENO, STDOUT_FILENO};
	const int signals[] = {SIGINT, SIGTERM, SIGHUP, SIGPIPE};
	struct sigaction saved[4], action = {0};
	sigset_t blocked, oldmask;
	struct termios uart_old, stdin_old, raw;
	bool exclusive = false, locked = false, uart_saved = false;
	bool stdin_saved = false, local_tty = false;
	struct queue tx = {{0}, 0}, rx = {{0}, 0};
	bool input_done = false, input_closing = false, uart_done = false, uart_closing = false;
	int64_t deadline = 0, quiet = 0;

	/* Reject all arguments before touching any file or terminal. */
	for (int i = 0; i < argc; i++) {
		if (!strcmp(argv[i], "--baud")) {
			if (have_baud || ++i == argc)
				return usage();
			baud = argv[i];
			have_baud = true;
		} else if (argv[i][0] == '-' || have_tty) {
			return usage();
		} else {
			tty = argv[i];
			have_tty = true;
		}
	}
	if (!baud_speed(baud, &speed)) {
		fprintf(stderr, "erbctl: console: unsupported baud: %s\n", baud);
		return usage();
	}

	/* Block managed signals during acquisition and restoration. A signal
	 * arriving during setup is delivered to our handler before entering poll. */
	sigemptyset(&blocked);
	for (size_t i = 0; i < sizeof(signals) / sizeof(signals[0]); i++)
		sigaddset(&blocked, signals[i]);
	if (sigprocmask(SIG_BLOCK, &blocked, &oldmask))
		return diagnostic("block signals");
	stopped = 0;
	sigemptyset(&action.sa_mask);
	for (; handlers < 4; handlers++) {
		action.sa_handler = signals[handlers] == SIGPIPE ? SIG_IGN : console_signal;
		if (sigaction(signals[handlers], &action, &saved[handlers])) {
			diagnostic("install signal handler");
			goto cleanup;
		}
	}
	uart = open(tty, O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC);
	if (uart < 0) { diagnostic(tty); goto cleanup; }
	if (flock(uart, LOCK_EX | LOCK_NB)) {
		diagnostic("UART busy (cooperative lock)");
		goto cleanup;
	}
	locked = true;
	if (ioctl(uart, TIOCEXCL)) { diagnostic("exclusive UART"); goto cleanup; }
	exclusive = true;
	if (tcgetattr(uart, &uart_old)) { diagnostic("UART termios"); goto cleanup; }
	uart_saved = true;
	struct stat uart_stat, stream_stat;
	if (fstat(uart, &uart_stat)) { diagnostic("UART stat"); goto cleanup; }
	for (int i = 0; i < 2; i++) {
		if (!fstat(streams[i], &stream_stat) && S_ISCHR(stream_stat.st_mode) &&
		    stream_stat.st_rdev == uart_stat.st_rdev) {
			fprintf(stderr, "erbctl: console: stdin/stdout must not be the UART\n");
			goto cleanup;
		}
	}
	raw = uart_old;
	cfmakeraw(&raw);
	raw.c_cflag &= ~(CSIZE | PARENB | PARODD | CSTOPB | CRTSCTS);
	raw.c_cflag |= CS8 | CLOCAL | CREAD;
	raw.c_iflag &= ~(IXON | IXOFF | IXANY | INPCK | IGNPAR);
	raw.c_cc[VMIN] = 1;
	raw.c_cc[VTIME] = 0;
	if (cfsetispeed(&raw, speed) || cfsetospeed(&raw, speed) ||
	    tcsetattr(uart, TCSANOW, &raw)) {
		diagnostic("configure UART");
		goto cleanup;
	}
	local_tty = isatty(STDIN_FILENO);
	if (local_tty) {
		if (tcgetattr(STDIN_FILENO, &stdin_old)) {
			diagnostic("stdin termios"); goto cleanup;
		}
		stdin_saved = true;
		raw = stdin_old;
		cfmakeraw(&raw);
		raw.c_iflag &= ~(IXON | IXOFF | IXANY);
		if (tcsetattr(STDIN_FILENO, TCSANOW, &raw)) {
			diagnostic("configure stdin"); goto cleanup;
		}
	}
	for (int i = 0; i < 2; i++) {
		flags[i] = fcntl(streams[i], F_GETFL);
		if (flags[i] < 0 || fcntl(streams[i], F_SETFL, flags[i] | O_NONBLOCK)) {
			diagnostic("nonblocking stdin/stdout"); goto cleanup;
		}
	}
	fprintf(stderr, "erbctl: console: %s at %s baud; Ctrl-] exits on terminal stdin\n",
		tty, baud);
	if (sigprocmask(SIG_SETMASK, &oldmask, NULL)) {
		diagnostic("unblock signals"); goto cleanup;
	}
	result = 0;
	while (!stopped) {
		int64_t now = milliseconds();
		struct pollfd p[3] = {
			{uart, 0, 0}, {-1, POLLIN, 0}, {-1, POLLOUT, 0}
		};
		int outq = 0;
		if (input_done && !uart_closing && !uart_done && !tx.len) {
			if (ioctl(uart, TIOCOUTQ, &outq)) {
				if (errno == EINTR) continue;
				result = diagnostic("UART output queue"); break;
			}
			if (outq) quiet = now + QUIET_MS;
		}
		if (deadline && now >= deadline) {
			if (tx.len || rx.len || outq || (!input_done && !uart_done)) {
				fprintf(stderr, "erbctl: console: drain timed out\n");
				result = 1;
			}
			break;
		}
		if (uart_done && !rx.len)
			break;
		bool finished = input_done && !uart_closing && !tx.len && !rx.len &&
			!outq && quiet && now >= quiet;
		if (!uart_done && rx.len < QUEUE_SIZE)
			p[0].events |= POLLIN;
		if (!uart_done && tx.len)
			p[0].events |= POLLOUT;
		/* Like stdin, observe UART HUP while its queue is full, once. */
		if (!p[0].events && (uart_closing || uart_done))
			p[0].fd = -1;
		/* Observe pipe closure even if TX is full, but latch HUP once so
		 * a permanently blocked UART cannot cause a HUP busy loop. */
		if (!input_done && !uart_done && (tx.len < QUEUE_SIZE || !input_closing))
			p[1].fd = STDIN_FILENO;
		if (tx.len == QUEUE_SIZE)
			p[1].events = 0;
		if (rx.len)
			p[2].fd = STDOUT_FILENO;
		/* Finite timeout closes the signal-before-poll race without a
		 * self-pipe, and bounds shutdown even under permanent backpressure. */
		/* Before ending the quiet grace period, probe once for UART bytes
		 * that may have accumulated while stdout was backpressured. */
		int n = poll(p, 3, finished ? 0 : 100);
		if (n < 0) {
			if (errno == EINTR) continue;
			result = diagnostic("poll"); break;
		}
		if (stopped) break;
		if (finished && n == 0) break;
		if ((p[0].revents | p[1].revents | p[2].revents) & POLLNVAL) {
			errno = EBADF; result = diagnostic("poll descriptor"); break;
		}
		if (p[2].revents & (POLLOUT | POLLERR | POLLHUP))
			if (write_queue(STDOUT_FILENO, &rx)) {
				result = diagnostic("stdout write"); break;
			}
		if (p[0].revents & (POLLHUP | POLLERR)) {
			if (!uart_closing)
				fprintf(stderr, "erbctl: console: UART disconnected\n");
			uart_closing = true;
			input_done = true;
			result = 1;
			tx.len = 0; /* Disconnected hardware cannot accept pending TX. */
			if (!deadline) deadline = milliseconds() + DRAIN_MS;
		}
		if (rx.len < QUEUE_SIZE && (p[0].revents & (POLLIN | POLLHUP | POLLERR))) {
			ssize_t count = read(uart, rx.bytes + rx.len, QUEUE_SIZE - rx.len);
			if (count > 0) {
				rx.len += (size_t)count;
				if (input_done) quiet = milliseconds() + QUIET_MS;
			} else if (!count || (count < 0 && errno == EIO)) {
				uart_done = true;
				result = 1;
				if (!deadline) deadline = milliseconds() + DRAIN_MS;
				if (!uart_closing)
					fprintf(stderr, "erbctl: console: UART disconnected\n");
			} else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
				result = diagnostic("UART read"); break;
			} else if ((errno == EAGAIN || errno == EWOULDBLOCK) && uart_closing) {
				uart_done = true;
			}
		}
		if (!uart_done && tx.len && (p[0].revents & POLLOUT))
			if (write_queue(uart, &tx)) {
				result = diagnostic("UART write"); break;
			}
		if (input_done && (p[0].revents & POLLOUT))
			quiet = milliseconds() + QUIET_MS;
		if (p[1].revents & (POLLHUP | POLLERR)) {
			input_closing = true;
			if (!deadline) deadline = milliseconds() + DRAIN_MS;
		}
		if (!input_done && tx.len < QUEUE_SIZE &&
		    (p[1].revents & (POLLIN | POLLHUP | POLLERR))) {
			ssize_t count = read(STDIN_FILENO, tx.bytes + tx.len, QUEUE_SIZE - tx.len);
			if (count > 0) {
				if (local_tty && memchr(tx.bytes + tx.len, 0x1d, (size_t)count))
					break;
				tx.len += (size_t)count;
			} else if (!count) {
				input_done = true;
				if (!deadline) deadline = milliseconds() + DRAIN_MS;
				quiet = milliseconds() + QUIET_MS;
			} else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
				result = diagnostic("stdin read"); break;
			}
		}
		/* Begin the reply grace period only after the last queued TX byte. */
		if (input_done && tx.len) quiet = milliseconds() + QUIET_MS;
	}
	if (stopped)
		result = 128 + stopped;

cleanup:
	/* TCSANOW deliberately avoids both RX flushing and an unbounded drain. */
	sigprocmask(SIG_BLOCK, &blocked, NULL);
	if (stdin_saved && tcsetattr(STDIN_FILENO, TCSANOW, &stdin_old))
		result = diagnostic("restore stdin termios");
	for (int i = 1; i >= 0; i--)
		if (flags[i] >= 0 && fcntl(streams[i], F_SETFL, flags[i]))
			result = diagnostic("restore stdin/stdout flags");
	if (uart_saved && tcsetattr(uart, TCSANOW, &uart_old))
		result = diagnostic("restore UART termios");
	if (exclusive && ioctl(uart, TIOCNXCL))
		result = diagnostic("clear exclusive UART");
	if (locked && flock(uart, LOCK_UN))
		result = diagnostic("unlock UART");
	if (uart >= 0) close(uart);
	while (handlers > 0) {
		handlers--;
		sigaction(signals[handlers], &saved[handlers], NULL);
	}
	sigprocmask(SIG_SETMASK, &oldmask, NULL);
	return result;
}
