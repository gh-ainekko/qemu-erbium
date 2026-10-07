/* SPDX-License-Identifier: Apache-2.0 */
/* Reusable non-Arduino, no-peripheral surface. Also usable by host VM tests. */
#include "mem.h"
#include "interp.h"

int useTFT;
int isCodingBox;
int isSpringbotGold;
int resetDoubleTap;
int mbDisplayColor;
int wifiActive;
char BLE_ThreeLetterID[4];
int BLE_allowShutdown;
int BLE_connected_to_IDE;
int bleRunning;


int pinCount(void) { return 0; }
int mapDigitalPinNum(int pin) { (void)pin; return -1; }
int inputOnlyPin(int pin) { (void)pin; return false; }
int hasI2CPullups(void) { return false; }
int displayIsActive(void) { return false; }
int readAnalogMicrophone(void) { return 0; }

void turnOffPins(void) {}
void updateMicrobitDisplay(void) {}

void resetRadio(void) {}
void stopPWM(void) {}
void stopServos(void) {}
void stopTone(void) {}
void turnOffInternalNeoPixels(void) {}
void tftClear(void) {}
void BLE_setEnabled(int flag) { (void)flag; }
int BLE_isEnabled(void) { return false; }
void BLE_start(void) {}
void BLE_stop(void) {}
int hasStartupSnapshot(void) { return false; }
void loadCodeSnapshot(char *name) { (void)name; }
void processFileMessage(int msgType, int size, char *data) {
    (void)msgType; (void)size; (void)data;
    /* No filesystem on this target. Matches upstream no-filesystem behavior. */
}

OBJ primAnalogPins(OBJ *args) { (void)args; return zeroObj; }
OBJ primDigitalPins(OBJ *args) { (void)args; return zeroObj; }
OBJ primButtonA(OBJ *args) { (void)args; return falseObj; }
OBJ primButtonB(OBJ *args) { (void)args; return falseObj; }
#define UNSUPPORTED_OBJ(name) \
    OBJ name(OBJ *args) { (void)args; return fail(primitiveNotImplemented); }
UNSUPPORTED_OBJ(primI2cGet)
UNSUPPORTED_OBJ(primI2cSet)
UNSUPPORTED_OBJ(primSPISend)
UNSUPPORTED_OBJ(primSPIRecv)
OBJ primAnalogRead(int count, OBJ *args) {
    (void)count; (void)args; return fail(primitiveNotImplemented);
}
OBJ primDigitalRead(int count, OBJ *args) {
    (void)count; (void)args; return fail(primitiveNotImplemented);
}
#define UNSUPPORTED_COMMAND(name) \
    void name(OBJ *args) { (void)args; fail(primitiveNotImplemented); }
UNSUPPORTED_COMMAND(primAnalogWrite)
UNSUPPORTED_COMMAND(primDigitalWrite)
/* Soft-reset calls these unconditionally. Absence must not poison errorCode. */
void primSetUserLED(OBJ *args) { (void)args; }
void primDigitalSet(int pin, int flag) {
    (void)pin; (void)flag; fail(primitiveNotImplemented);
}
OBJ primMBDisplayOff(int count, OBJ *args) {
    (void)count; (void)args; return falseObj;
}
