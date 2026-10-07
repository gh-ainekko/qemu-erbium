/* SPDX-License-Identifier: Apache-2.0 */
/* Shared-library callback deliberately loaded outside the compact VM arena. */
#include <stdint.h>
uint32_t native_test_primitive(int count, uint32_t *args) {
    uint32_t sum = count ? 0 : 42;
    for (int i = 0; i < count; i++) sum += args[i] >> 1;
    return (sum << 1) | 1;
}
