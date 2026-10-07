/* SPDX-License-Identifier: GPL-2.0 */
#ifndef ERBIUM_CONSOLE_H
#define ERBIUM_CONSOLE_H

/* Direct physical UART console; never opens the xSPI control device. */
int erbctl_console(int argc, char **argv);

#endif
