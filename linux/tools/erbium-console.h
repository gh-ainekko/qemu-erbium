/* SPDX-License-Identifier: GPL-2.0 */
#ifndef ERBIUM_CONSOLE_H
#define ERBIUM_CONSOLE_H

/* Direct UART console. Only the optional --load child opens control/MTD. */
int erbctl_console(const char *device, int argc, char **argv);

#endif
