// SPDX-License-Identifier: Apache-2.0
// Compile the actual device with only the System/PLIC boundary stubbed.
// There is no replacement UART implementation or IDE protocol in this harness.
#include <cassert>
#include <chrono>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <vector>
#include <fcntl.h>
#include <pty.h>
#include <sys/ioctl.h>
#include <sys/resource.h>
#include <termios.h>
#include <unistd.h>

#define BEMU_SYSTEM_H
namespace bemu {
class System {
public:
    bool enabled = true;
    bool irq = false;
    bool is_uart_enabled() const { return enabled; }
    void er_plic_interrupt_pending_set(uint32_t) { irq = true; }
    void er_plic_interrupt_pending_clear(uint32_t) { irq = false; }
};
}
#include "devices/shakti_uart.h"

extern "C" ssize_t __real_write(int, const void*, size_t);
extern "C" ssize_t __real_read(int, void*, size_t);
static int inject_tx = -1, inject_rx = -1;
static int tx_eintr = 0, tx_eagain = 0, tx_zero = 0, rx_eintr = 0, rx_eagain = 0;
static size_t tx_limit = SIZE_MAX, rx_calls = 0;
extern "C" ssize_t __wrap_write(int fd, const void* data, size_t count) {
    if (fd == inject_tx) {
        if (tx_eintr-- > 0) { errno = EINTR; return -1; }
        if (tx_eagain-- > 0) { errno = EAGAIN; return -1; }
        if (tx_zero-- > 0) return 0;
        count = std::min(count, tx_limit);
    }
    return __real_write(fd, data, count);
}
extern "C" ssize_t __wrap_read(int fd, void* data, size_t count) {
    if (fd == inject_rx) {
        ++rx_calls;
        if (rx_eintr-- > 0) { errno = EINTR; return -1; }
        if (rx_eagain-- > 0) { errno = EAGAIN; return -1; }
    }
    return __real_read(fd, data, count);
}

using Uart = bemu::ShaktiUart<0x02004000, 0x1000, 3>;
static bemu::System chip;
static bemu::Noagent agent(&chip);
static uint32_t rd(Uart& uart, uint64_t reg) {
    uint32_t value = 0;
    uart.read(agent, reg, 4, reinterpret_cast<uint8_t*>(&value));
    return value;
}
static void wr(Uart& uart, uint64_t reg, uint32_t value) {
    uart.write(agent, reg, 4, reinterpret_cast<const uint8_t*>(&value));
}
static uint32_t status(Uart& uart) { return rd(uart, Uart::SHAKTI_UART_STATUS); }
static void tick(Uart& uart) { uart.clock_tick(agent, Uart::UART_CLK_DIV); }
static void nb(int fd) {
    assert(fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK) == 0);
}
static std::vector<uint8_t> drain(int fd) {
    std::vector<uint8_t> result;
    uint8_t buf[8192];
    for (;;) {
        const ssize_t n = read(fd, buf, sizeof(buf));
        if (n <= 0) { assert(n == 0 || errno == EAGAIN); break; }
        result.insert(result.end(), buf, buf+n);
    }
    return result;
}

static void rx_burst() {
    int pipefd[2];
    assert(pipe(pipefd) == 0);
    Uart uart;
    uart.rx_fd = pipefd[0]; // starts blocking; the device must make it safe
    inject_rx = pipefd[0];
    rx_eintr = 1;
    assert(!(status(uart) & Uart::STATUS_RX_NOT_EMPTY));
    assert(fcntl(pipefd[0], F_GETFL) & O_NONBLOCK);
    std::vector<uint8_t> input(4096);
    for (size_t i = 0; i < input.size(); ++i) input[i] = (i * 73 + i / 256) & 255;
    assert(write(pipefd[1], input.data(), input.size()) == static_cast<ssize_t>(input.size()));
    rx_eagain = 1;
    assert(!(status(uart) & Uart::STATUS_RX_NOT_EMPTY));
    assert(status(uart) & Uart::STATUS_RX_FULL);
    const size_t calls = rx_calls;
    for (int i = 0; i < 100; ++i) {
        tick(uart);
        assert(status(uart) & Uart::STATUS_RX_FULL);
        assert(!(status(uart) & Uart::STATUS_OVERRUN));
    }
    assert(rx_calls == calls); // a full FIFO MUST NOT consume host data
    int waiting = 0;
    assert(ioctl(pipefd[0], FIONREAD, &waiting) == 0 && waiting == 4080);
    wr(uart, Uart::SHAKTI_UART_RX_THRESHOLD, 15);
    wr(uart, Uart::SHAKTI_UART_IEN, Uart::STATUS_RXFIFOTHRE);
    assert(chip.irq);
    for (uint8_t expected : input) {
        assert(status(uart) & Uart::STATUS_RX_NOT_EMPTY);
        assert(rd(uart, Uart::SHAKTI_UART_RCV_REG) == expected);
    }
    assert(!(status(uart) & Uart::STATUS_RX_NOT_EMPTY));
    // Built RTL latches threshold STATUS and RAW separately. Emptying RX does
    // not acknowledge an already-raised interrupt; clear both sticky states.
    assert(chip.irq);
    wr(uart, Uart::SHAKTI_UART_STATUS, 0);
    wr(uart, Uart::SHAKTI_UART_RAW, rd(uart, Uart::SHAKTI_UART_RAW));
    assert(!chip.irq);
    close(pipefd[1]);
    assert(!(status(uart) & Uart::STATUS_RX_NOT_EMPTY));
    assert(uart.rx_fd == pipefd[0]); // EOF does not lose ownership/cleanup fd
    close(pipefd[0]);
    inject_rx = -1;
    std::cout << "PASS RX 4096 binary bytes, bounded fullness, EINTR/EAGAIN, IRQ, EOF\n";
}

static void tx_backpressure() {
    int pipefd[2];
    assert(pipe(pipefd) == 0);
    nb(pipefd[0]);
    nb(pipefd[1]);
    std::vector<uint8_t> filler(4096, 0x55);
    while (write(pipefd[1], filler.data(), filler.size()) > 0) {}
    assert(errno == EAGAIN);
    Uart uart;
    uart.tx_fd = pipefd[1];
    for (unsigned i = 0; i < 16; ++i) wr(uart, Uart::SHAKTI_UART_TX_REG, i * 17);
    tick(uart); // REAL kernel EAGAIN
    assert(status(uart) & Uart::STATUS_TX_FULL);
    assert(!(status(uart) & Uart::STATUS_TX_EMPTY));
    drain(pipefd[0]);
    inject_tx = pipefd[1];
    tx_eintr = 1;
    tx_eagain = 1;
    tick(uart); // EINTR retry, then EAGAIN
    assert(status(uart) & Uart::STATUS_TX_FULL);
    tx_zero = 1;
    tick(uart);
    assert(status(uart) & Uart::STATUS_TX_FULL);
    tx_limit = 3;
    std::vector<uint8_t> expected;
    for (unsigned i = 0; i < 16; ++i) expected.push_back(i * 17);
    tick(uart); // accept three, retain thirteen; force ring wrap
    assert(!(status(uart) & (Uart::STATUS_TX_FULL | Uart::STATUS_TX_EMPTY)));
    for (unsigned i = 0; i < 3; ++i) {
        wr(uart, Uart::SHAKTI_UART_TX_REG, 250+i);
        expected.push_back(250+i);
    }
    assert(status(uart) & Uart::STATUS_TX_FULL);
    for (unsigned i = 0; i < 20; ++i) tick(uart);
    assert(status(uart) & Uart::STATUS_TX_EMPTY);
    assert(drain(pipefd[0]) == expected);
    inject_tx = -1;
    tx_limit = SIZE_MAX;
    close(pipefd[0]);
    wr(uart, Uart::SHAKTI_UART_TX_REG, 0xff);
    tick(uart); // EPIPE with the default SIGPIPE disposition must not kill us
    assert(!(status(uart) & Uart::STATUS_TX_EMPTY));
    sigset_t mask, original, pending;
    sigemptyset(&mask);
    sigaddset(&mask, SIGPIPE);
    assert(pthread_sigmask(SIG_BLOCK, &mask, &original) == 0);
    assert(raise(SIGPIPE) == 0);
    tick(uart);
    assert(sigpending(&pending) == 0 && sigismember(&pending, SIGPIPE) == 1);
    const struct timespec zero = {0, 0};
    assert(sigtimedwait(&mask, nullptr, &zero) == SIGPIPE);
    assert(pthread_sigmask(SIG_SETMASK, &original, nullptr) == 0);
    uart.tx_fd = -1;
    tick(uart);
    close(pipefd[1]);
    assert(pipe(pipefd) == 0);
    nb(pipefd[0]);
    uart.tx_fd = pipefd[1];
    tick(uart);
    assert(fcntl(pipefd[1], F_GETFL) & O_NONBLOCK);
    assert(drain(pipefd[0]) == std::vector<uint8_t>{0xff});
    close(pipefd[0]);
    close(pipefd[1]);
    std::cout << "PASS TX kernel EAGAIN, EINTR, zero/partial write, wrap, EPIPE/resume, SIGPIPE isolation\n";
}

static void reconnect_and_high_fd() {
    struct rlimit limit;
    assert(getrlimit(RLIMIT_NOFILE, &limit) == 0);
    if (limit.rlim_cur < 4096) {
        limit.rlim_cur = 4096;
        assert(setrlimit(RLIMIT_NOFILE, &limit) == 0);
    }
    char dir[] = "/tmp/uart-transport-XXXXXX";
    assert(mkdtemp(dir));
    const std::string path = std::string(dir) + "/rx";
    assert(mkfifo(path.c_str(), 0600) == 0);
    const int lowfd = open(path.c_str(), O_RDONLY | O_NONBLOCK);
    assert(lowfd >= 0);
    const int fd = fcntl(lowfd, F_DUPFD, 2048); // beyond select FD_SETSIZE
    assert(fd >= 2048);
    close(lowfd);
    Uart uart;
    uart.rx_fd = fd;
    assert(!(status(uart) & Uart::STATUS_RX_NOT_EMPTY)); // no writer: EOF
    for (int i = 0; i < 3; ++i) {
        int writer = open(path.c_str(), O_WRONLY | O_NONBLOCK);
        assert(writer >= 0);
        const uint8_t data[] = {0, 255, static_cast<uint8_t>(i)};
        assert(write(writer, data, sizeof(data)) == sizeof(data));
        close(writer); // queued bytes must survive EOF
        for (uint8_t byte : data) {
            assert(status(uart) & Uart::STATUS_RX_NOT_EMPTY);
            assert(rd(uart, Uart::SHAKTI_UART_RCV_REG) == byte);
        }
        assert(!(status(uart) & Uart::STATUS_RX_NOT_EMPTY));
        assert(uart.rx_fd == fd);
    }
    close(fd);
    bool bad_fd = false;
    try { status(uart); }
    catch (const std::system_error& error) { bad_fd = error.code().value() == EBADF; }
    assert(bad_fd); // permanent configuration errors are not silently hidden
    unlink(path.c_str());
    rmdir(dir);
    std::cout << "PASS FIFO EOF/reconnect, binary bytes, high fd, permanent-error reporting\n";
}

static void pty_slave_reconnect() {
    int master, slave;
    assert(openpty(&master, &slave, nullptr, nullptr, nullptr) == 0);
    struct termios attrs;
    assert(tcgetattr(slave, &attrs) == 0);
    cfmakeraw(&attrs);
    assert(tcsetattr(slave, TCSANOW, &attrs) == 0);
    const std::string path = ttyname(slave);
    Uart uart;
    uart.rx_fd = master;
    close(slave);
    assert(!(status(uart) & Uart::STATUS_RX_NOT_EMPTY)); // EIO, not fatal detach
    assert(uart.rx_fd == master);
    slave = open(path.c_str(), O_RDWR | O_NOCTTY | O_NONBLOCK);
    assert(slave >= 0);
    const uint8_t data[] = {0, 0xff, 0xfa, 0x11, 0x13};
    assert(write(slave, data, sizeof(data)) == sizeof(data));
    for (uint8_t expected : data) {
        assert(status(uart) & Uart::STATUS_RX_NOT_EMPTY);
        assert(rd(uart, Uart::SHAKTI_UART_RCV_REG) == expected);
    }
    assert(!(status(uart) & Uart::STATUS_RX_NOT_EMPTY));
    close(slave);
    close(master);
    std::cout << "PASS raw PTY EIO and slave-peer reconnect\n";
}

int main() {
    const auto start = std::chrono::steady_clock::now();
    rx_burst();
    tx_backpressure();
    reconnect_and_high_fd();
    pty_slave_reconnect();
    assert(std::chrono::steady_clock::now()-start < std::chrono::seconds(5));
}
