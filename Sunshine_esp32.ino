#include <Wire.h>
#include <Adafruit_INA219.h>
#include <ESP32Servo.h>
#include <ctype.h>
#include <string.h>
#include <stdlib.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// =====================================================
// CONTROL PHILOSOPHY
// =====================================================
// 1. Conveyor AUTOMATIC mode = fixed cycle: 2 s RUN / 5 s STOP (restored).
//    The energy logic never touches the conveyor.
// 2. EV indicator = shiftable load. It turns RED (deferred) as soon as ANY
//    panel is confirmed low, and returns to GREEN last, after a hold time.
// 3. Battery charger permission = stopped only when ALL panels are low, and
//    restored after a stable hold time.
// 4. Panel readings are debounced (several consecutive samples), on top of
//    the 2.0 V low / 2.3 V recover hysteresis.
// 5. The relay switches the CHARGER INPUT, so in AUTOMATIC mode it follows
//    the charging permission: ON = charging allowed, OFF = charging stopped.
//    Set SOLAR_CONTROLS_RELAY = false below to go back to the 3 s demo toggle.
//
// Telemetry text keeps every existing label so factory3d.html still parses it.


// =====================================================
// PIN CONFIGURATION
// =====================================================

#define RELAY_PIN 5
#define SDA_PIN 18
#define SCL_PIN 19

const int solarPin1 = 32;
const int solarPin2 = 35;
const int solarPin3 = 34;

const int servoPin = 27;

const int greenLedPin = 22;
const int redLedPin = 23;


// =====================================================
// HARDWARE OBJECTS
// =====================================================

Adafruit_INA219 ina219(0x40);
Servo conveyorServo;

bool ina219Ready = false;
bool ina219HasReading = false;


// =====================================================
// BLUETOOTH LOW ENERGY (Nordic UART style)
// =====================================================

#define BLE_NAME     "SunShine-ESP32"
#define NUS_SERVICE  "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
#define NUS_RX       "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"   // browser -> ESP32
#define NUS_TX       "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"   // ESP32 -> browser

BLEServer *bleServer = nullptr;
BLECharacteristic *bleTx = nullptr;
volatile bool bleConnected = false;
volatile uint16_t bleChunk = 20;

const size_t BLE_OUT_SIZE = 4096;
uint8_t bleOut[BLE_OUT_SIZE];
volatile size_t bleOutHead = 0;
volatile size_t bleOutTail = 0;

const size_t BLE_IN_SIZE = 128;
char bleIn[BLE_IN_SIZE];
volatile size_t bleInHead = 0;
volatile size_t bleInTail = 0;

void bleQueue(uint8_t c) {
  if (!bleConnected) {
    return;
  }
  size_t next = (bleOutHead + 1) % BLE_OUT_SIZE;
  if (next == bleOutTail) {
    return;  // buffer full: drop
  }
  bleOut[bleOutHead] = c;
  bleOutHead = next;
}

class DualPrint : public Print {
public:
  size_t write(uint8_t c) override {
    Serial.write(c);
    bleQueue(c);
    return 1;
  }
  size_t write(const uint8_t *buf, size_t n) override {
    Serial.write(buf, n);
    for (size_t i = 0; i < n; i++) {
      bleQueue(buf[i]);
    }
    return n;
  }
};

DualPrint Out;

class ServerCb : public BLEServerCallbacks {
  void onConnect(BLEServer *server) {
    bleChunk = 20;
    bleConnected = true;
  }
  void onDisconnect(BLEServer *server) {
    bleConnected = false;
    BLEDevice::startAdvertising();
  }
  void onMtuChanged(BLEServer *server, esp_ble_gatts_cb_param_t *param) {
    int m = param->mtu.mtu;
    if (m > 23) {
      bleChunk = (m - 3 > 180) ? 180 : (m - 3);
    }
  }
};

class RxCb : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *c) {
    auto v = c->getValue();
    for (size_t i = 0; i < v.length(); i++) {
      size_t next = (bleInHead + 1) % BLE_IN_SIZE;
      if (next != bleInTail) {
        bleIn[bleInHead] = (char)v[i];
        bleInHead = next;
      }
    }
  }
};

void setupBle() {
  BLEDevice::init(BLE_NAME);
  BLEDevice::setMTU(185);

  bleServer = BLEDevice::createServer();
  bleServer->setCallbacks(new ServerCb());

  BLEService *svc = bleServer->createService(NUS_SERVICE);

  bleTx = svc->createCharacteristic(NUS_TX, BLECharacteristic::PROPERTY_NOTIFY);
  bleTx->addDescriptor(new BLE2902());

  BLECharacteristic *rx = svc->createCharacteristic(
    NUS_RX,
    BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  rx->setCallbacks(new RxCb());

  svc->start();

  BLEAdvertising *adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(NUS_SERVICE);
  adv->setScanResponse(true);
  BLEDevice::startAdvertising();
}

// Send queued text to the browser a few chunks per loop pass.
void bleFlush() {
  if (!bleConnected) {
    bleOutTail = bleOutHead;
    return;
  }

  int sent = 0;
  while (bleOutTail != bleOutHead && sent < 6) {
    uint8_t tmp[180];
    size_t n = 0;
    size_t chunk = bleChunk;
    if (chunk > sizeof(tmp)) {
      chunk = sizeof(tmp);
    }
    while (n < chunk && bleOutTail != bleOutHead) {
      tmp[n++] = bleOut[bleOutTail];
      bleOutTail = (bleOutTail + 1) % BLE_OUT_SIZE;
    }
    bleTx->setValue(tmp, n);
    bleTx->notify();
    sent++;
    delay(4);
  }
}


// =====================================================
// RELAY CONFIGURATION
// =====================================================
// Relay ON  = GPIO5 OUTPUT LOW
// Relay OFF = GPIO5 INPUT (high impedance)

bool relayState = false;
bool relayAutoMode = true;

unsigned long lastRelayToggle = 0;
const unsigned long RELAY_INTERVAL = 3000;      // demo toggle period (only when SOLAR_CONTROLS_RELAY is false)
const unsigned long RELAY_MIN_SWITCH_MS = 2000; // never switch the relay faster than this


// =====================================================
// CONVEYOR SERVO CONFIGURATION
// =====================================================
// Continuous-rotation servo: 90 degrees = stop (1500 us),
// moving away from 90 = faster. Speed is a percentage 1-100.
// 23 % is about the old fixed value of 70 degrees.

const int SERVO_STOP_ANGLE = 90;
int stopPulse = 1500;
int conveyorSpeedPct = 23;

bool conveyorRunning = true;
bool conveyorAutoMode = true;   // AUTOMATIC = 2 s RUN / 5 s STOP cycle

unsigned long lastConveyorChange = 0;
const unsigned long conveyorRunTime = 2000;
const unsigned long conveyorStopTime = 5000;


// =====================================================
// LED CONFIGURATION
// =====================================================

bool ledAutoMode = true;     // true = LEDs follow the energy logic
bool ledGreenOn = false;
bool ledRedOn = false;


// =====================================================
// SOLAR PANEL READINGS
// =====================================================

const float PANEL_LOW_THRESHOLD = 2.0;
const float PANEL_RECOVER_THRESHOLD = 2.3;

bool panel1Low = false;
bool panel2Low = false;
bool panel3Low = false;

bool panel1Initialized = false;
bool panel2Initialized = false;
bool panel3Initialized = false;

uint8_t panel1Count = 0;
uint8_t panel2Count = 0;
uint8_t panel3Count = 0;

int solarRaw1 = 0;
int solarRaw2 = 0;
int solarRaw3 = 0;

float solarVoltage1 = 0.0;
float solarVoltage2 = 0.0;
float solarVoltage3 = 0.0;

bool sensorSaturated = false;   // true when an ADC pin is close to its limit

unsigned long lastSolarRead = 0;
const unsigned long SOLAR_INTERVAL = 200;


// =====================================================
// INA219 MEASUREMENTS
// =====================================================

float inaBusVoltage = 0.0;
float inaShuntVoltage = 0.0;
float inaCurrent_mA = 0.0;
float inaPower_mW = 0.0;
float inaLoadVoltage = 0.0;

unsigned long lastInaRead = 0;
const unsigned long INA_INTERVAL = 1000;

unsigned long lastStatusPrint = 0;
const unsigned long STATUS_INTERVAL = 1000;


// =====================================================
// SERIAL COMMAND INPUT
// =====================================================

char commandBuffer[40];
uint8_t commandLength = 0;


// ENERGY LOGIC BEGIN (this block is also compiled on a PC for testing)
// =====================================================
// ENERGY POLICY AND STATE MACHINE
// =====================================================
const bool SOLAR_CONTROLS_RELAY = true;

// A panel must read low/healthy for this many 200 ms samples in a row.
const uint8_t LOW_CONFIRM_SAMPLES = 3;       // 0.6 s
const uint8_t RECOVER_CONFIRM_SAMPLES = 5;   // 1.0 s

// Recovery order: charging first, EV indicator last.
const unsigned long CHARGE_RESTORE_HOLD_MS = 3000;  // not-all-low for 3 s
const unsigned long EV_RESTORE_HOLD_MS = 6000;      // all panels healthy for 6 s

enum EnergyState {
  ES_NORMAL,
  ES_ONE_PANEL_SHADED,
  ES_TWO_PANELS_SHADED,
  ES_ALL_PANELS_SHADED,
  ES_RECOVERY
};

// Safe start-up defaults: EV deferred, charging not permitted.
bool evAllowed = false;
bool chargeAllowed = false;
bool notAllLowTracking = false;
bool allHealthyTracking = false;
unsigned long notAllLowSince = 0;
unsigned long allHealthySince = 0;
EnergyState energyState = ES_RECOVERY;
EnergyState lastReportedState = ES_RECOVERY;

// Takes a plain int so the Arduino IDE's automatic prototype (placed above
// the enum) compiles.
const char *energyStateName(int s) {
  switch (s) {
    case ES_NORMAL:             return "NORMAL";
    case ES_ONE_PANEL_SHADED:   return "ONE_PANEL_SHADED";
    case ES_TWO_PANELS_SHADED:  return "TWO_PANELS_SHADED";
    case ES_ALL_PANELS_SHADED:  return "ALL_PANELS_SHADED";
    default:                    return "RECOVERY";
  }
}

// Hysteresis (2.0 V low / 2.3 V recover) plus a consecutive-sample counter.
bool updatePanelLowState(float voltage,
                         bool currentLow,
                         bool &initialised,
                         uint8_t &counter,
                         const char *panelName) {
  if (!initialised) {
    initialised = true;
    counter = 0;
    return (voltage < PANEL_LOW_THRESHOLD);
  }

  if (!currentLow) {
    if (voltage < PANEL_LOW_THRESHOLD) {
      if (++counter >= LOW_CONFIRM_SAMPLES) {
        counter = 0;
        Out.print("WARNING: ");
        Out.print(panelName);
        Out.println(" LOW LIGHT!");
        return true;
      }
    } else {
      counter = 0;
    }
  } else {
    if (voltage > PANEL_RECOVER_THRESHOLD) {
      if (++counter >= RECOVER_CONFIRM_SAMPLES) {
        counter = 0;
        Out.print(panelName);
        Out.println(" LIGHT RECOVERED!");
        return false;
      }
    } else {
      counter = 0;
    }
  }

  return currentLow;
}

// Decides EV permission, charging permission and the reported state.
// Never touches the conveyor.
void updateEnergyLogic(unsigned long now) {
  int lowCount = (panel1Low ? 1 : 0) + (panel2Low ? 1 : 0) + (panel3Low ? 1 : 0);
  bool anyLow = (lowCount > 0);
  bool allLow = (lowCount == 3);

  // Charging permission: stop only when ALL panels are low; restore after a hold.
  if (allLow) {
    if (chargeAllowed) {
      Out.println("Charging permission: STOPPED (all panels low)");
    }
    chargeAllowed = false;
    notAllLowTracking = false;
  } else {
    if (!notAllLowTracking) {
      notAllLowTracking = true;
      notAllLowSince = now;
    }
    if (!chargeAllowed && (now - notAllLowSince >= CHARGE_RESTORE_HOLD_MS)) {
      chargeAllowed = true;
      Out.println("Charging permission: RESTORED (input stable)");
    }
  }

  // EV load: deferred at once when ANY panel is low; restored last.
  if (anyLow) {
    if (evAllowed) {
      Out.println("EV load: DEFERRED (solar availability dropped)");
    }
    evAllowed = false;
    allHealthyTracking = false;
  } else {
    if (!allHealthyTracking) {
      allHealthyTracking = true;
      allHealthySince = now;
    }
    if (!evAllowed && chargeAllowed && (now - allHealthySince >= EV_RESTORE_HOLD_MS)) {
      evAllowed = true;
      Out.println("EV load: ENABLED (stable surplus confirmed)");
    }
  }

  // Reported state.
  if (lowCount == 3) {
    energyState = ES_ALL_PANELS_SHADED;
  } else if (lowCount == 2) {
    energyState = ES_TWO_PANELS_SHADED;
  } else if (lowCount == 1) {
    energyState = ES_ONE_PANEL_SHADED;
  } else {
    energyState = (evAllowed && chargeAllowed) ? ES_NORMAL : ES_RECOVERY;
  }

  if (energyState != lastReportedState) {
    lastReportedState = energyState;
    Out.print("Energy state change: ");
    Out.println(energyStateName(energyState));
  }
}
// ENERGY LOGIC END


// =====================================================
// LED CONTROL
// =====================================================

void applyLeds(bool green, bool red) {
  ledGreenOn = green;
  ledRedOn = red;
  digitalWrite(greenLedPin, green ? HIGH : LOW);
  digitalWrite(redLedPin, red ? HIGH : LOW);
}


void printLedState() {
  Out.print("LED control: ");
  Out.println(ledAutoMode ? "AUTOMATIC" : "MANUAL");

  Out.print("LED output: green=");
  Out.print(ledGreenOn ? "ON" : "OFF");
  Out.print(" red=");
  Out.println(ledRedOn ? "ON" : "OFF");
}


// =====================================================
// RELAY CONTROL
// =====================================================

void setRelay(bool turnOn) {
  if (turnOn) {
    digitalWrite(RELAY_PIN, LOW);
    pinMode(RELAY_PIN, OUTPUT);
  } else {
    pinMode(RELAY_PIN, INPUT);
  }

  relayState = turnOn;
}


void printRelayState() {
  Out.print("Relay: ");
  Out.println(relayState ? "ON" : "OFF");

  Out.println(relayState
                   ? "GPIO5: OUTPUT LOW"
                   : "GPIO5: INPUT (DISCONNECTED)");

  Out.print("Relay control: ");
  Out.println(relayAutoMode ? "AUTOMATIC" : "MANUAL");
}


void updateRelay(unsigned long now) {
  if (!relayAutoMode) {
    return;
  }

  if (SOLAR_CONTROLS_RELAY) {
    // Relay follows the charging permission, never faster than RELAY_MIN_SWITCH_MS.
    if (relayState != chargeAllowed && now - lastRelayToggle >= RELAY_MIN_SWITCH_MS) {
      lastRelayToggle = now;
      setRelay(chargeAllowed);

      Out.print("Automatic relay toggle -> ");
      Out.println(relayState ? "ON" : "OFF");
      Out.println(relayState
                       ? "GPIO5: OUTPUT LOW"
                       : "GPIO5: INPUT (DISCONNECTED)");
    }
    return;
  }

  // Old demo behaviour.
  if (now - lastRelayToggle >= RELAY_INTERVAL) {
    lastRelayToggle = now;
    setRelay(!relayState);

    Out.print("Automatic relay toggle -> ");
    Out.println(relayState ? "ON" : "OFF");
    Out.println(relayState
                     ? "GPIO5: OUTPUT LOW"
                     : "GPIO5: INPUT (DISCONNECTED)");
  }
}


// =====================================================
// CONVEYOR CONTROL
// =====================================================

int speedToAngle(int pct) {
  return SERVO_STOP_ANGLE - (pct * SERVO_STOP_ANGLE) / 100;
}


void setConveyorState(bool run) {
  conveyorRunning = run;

  if (conveyorRunning) {
    conveyorServo.write(speedToAngle(conveyorSpeedPct));
  } else {
    conveyorServo.writeMicroseconds(stopPulse);
  }
}


// AUTOMATIC mode: 2 s RUN / 5 s STOP cycle, independent of the solar state.
void updateConveyor(unsigned long now) {
  if (!conveyorAutoMode) {
    return;
  }

  unsigned long requiredInterval = conveyorRunning
                                     ? conveyorRunTime
                                     : conveyorStopTime;

  if (now - lastConveyorChange >= requiredInterval) {
    lastConveyorChange = now;
    setConveyorState(!conveyorRunning);

    Out.print("Automatic conveyor: ");
    Out.println(conveyorRunning ? "RUNNING" : "STOPPED");
  }
}


// =====================================================
// SOLAR SENSORS
// =====================================================

void updateSolarSensors() {
  solarRaw1 = analogRead(solarPin1);
  solarRaw2 = analogRead(solarPin2);
  solarRaw3 = analogRead(solarPin3);

  float adcVoltage1 = analogReadMilliVolts(solarPin1) / 1000.0;
  float adcVoltage2 = analogReadMilliVolts(solarPin2) / 1000.0;
  float adcVoltage3 = analogReadMilliVolts(solarPin3) / 1000.0;

  // The ESP32 ADC (11 dB) tops out near 3.1 V. If a pin is this close, the
  // divider is clipping and the reading cannot be trusted.
  sensorSaturated = (adcVoltage1 > 3.0 || adcVoltage2 > 3.0 || adcVoltage3 > 3.0);

  solarVoltage1 = adcVoltage1 * 2.0;
  solarVoltage2 = adcVoltage2 * 2.0;
  solarVoltage3 = adcVoltage3 * 2.0;

  panel1Low = updatePanelLowState(
    solarVoltage1, panel1Low, panel1Initialized, panel1Count, "PANEL 1");

  panel2Low = updatePanelLowState(
    solarVoltage2, panel2Low, panel2Initialized, panel2Count, "PANEL 2");

  panel3Low = updatePanelLowState(
    solarVoltage3, panel3Low, panel3Initialized, panel3Count, "PANEL 3");

  updateEnergyLogic(millis());

  // The EV LEDs follow the energy logic only in automatic mode:
  // GREEN = EV enabled, RED = EV deferred.
  if (ledAutoMode) {
    applyLeds(evAllowed, !evAllowed);
  }
}


// =====================================================
// INA219 MONITORING
// =====================================================

void readINA219() {
  inaBusVoltage = ina219.getBusVoltage_V();
  inaShuntVoltage = ina219.getShuntVoltage_mV();
  inaCurrent_mA = ina219.getCurrent_mA();
  inaPower_mW = ina219.getPower_mW();

  inaLoadVoltage = inaBusVoltage + (inaShuntVoltage / 1000.0);
  ina219HasReading = true;
}


// =====================================================
// I2C SCANNER
// =====================================================

void scanI2C() {
  Out.println("\nScanning I2C devices...");

  int foundDevices = 0;

  for (byte address = 1; address < 127; address++) {
    Wire.beginTransmission(address);
    byte error = Wire.endTransmission();

    if (error == 0) {
      Out.print("Found device at address: 0x");
      if (address < 16) {
        Out.print("0");
      }
      Out.println(address, HEX);
      foundDevices++;
    }
  }

  if (foundDevices == 0) {
    Out.println("No I2C devices found!");
  }

  Out.println("I2C scan completed.\n");
}


// =====================================================
// STATUS OUTPUT
// =====================================================

void printSystemStatus() {
  Out.println("\n========== SYSTEM STATUS ==========");

  Out.print("Panel 1 (GPIO32): ");
  Out.print(solarVoltage1, 2);
  Out.print(" V | ");
  Out.println(panel1Low ? "LOW LIGHT" : "LIGHT DETECTED");

  Out.print("Panel 2 (GPIO35): ");
  Out.print(solarVoltage2, 2);
  Out.print(" V | ");
  Out.println(panel2Low ? "LOW LIGHT" : "LIGHT DETECTED");

  Out.print("Panel 3 (GPIO34): ");
  Out.print(solarVoltage3, 2);
  Out.print(" V | ");
  Out.println(panel3Low ? "LOW LIGHT" : "LIGHT DETECTED");

  Out.print("Solar LED status: ");
  Out.println(evAllowed ? "GREEN - EV ENABLED" : "RED - EV DEFERRED");
  printLedState();

  Out.print("Energy state: ");
  Out.println(energyStateName(energyState));
  Out.print("EV load: ");
  Out.println(evAllowed ? "ENABLED" : "DEFERRED");
  Out.print("Charging permission: ");
  Out.println(chargeAllowed ? "ALLOWED" : "STOPPED");
  Out.print("Relay logic: ");
  Out.println(SOLAR_CONTROLS_RELAY ? "SOLAR (relay ON = charging)"
                                   : "DEMO TOGGLE (solar control OFF)");
  Out.print("Sensor quality: ");
  Out.println(sensorSaturated ? "WARNING - ADC near limit, check divider" : "OK");

  printRelayState();

  Out.print("Conveyor: ");
  Out.println(conveyorRunning ? "RUNNING" : "STOPPED");
  Out.print("Conveyor control: ");
  Out.println(conveyorAutoMode ? "AUTOMATIC" : "MANUAL");
  Out.print("Conveyor speed: ");
  Out.print(conveyorSpeedPct);
  Out.println("%");
  Out.println("Conveyor policy: automatic 2 s RUN / 5 s STOP cycle");

  Out.println("------ INA219 READINGS ------");
  if (!ina219Ready) {
    Out.println("INA219 unavailable - check I2C wiring/address.");
  } else if (!ina219HasReading) {
    Out.println("Waiting for first INA219 reading...");
  } else {
    Out.print("Bus Voltage (VIN-): ");
    Out.print(inaBusVoltage, 3);
    Out.println(" V");

    Out.print("Shunt Voltage: ");
    Out.print(inaShuntVoltage, 3);
    Out.println(" mV");

    Out.print("Load Voltage (estimated VIN+): ");
    Out.print(inaLoadVoltage, 3);
    Out.println(" V");

    Out.print("Current: ");
    Out.print(inaCurrent_mA, 2);
    Out.println(" mA");

    Out.print("Power: ");
    Out.print(inaPower_mW, 2);
    Out.println(" mW");
  }

  Out.println("==================================");
}


// =====================================================
// SERIAL COMMANDS
// =====================================================

void printHelp() {
  Out.println("\n========== SERIAL COMMANDS ==========");
  Out.println("STATUS            - Show all current readings/states");
  Out.println("RELAY ON          - Force relay ON (GPIO5 OUTPUT LOW)");
  Out.println("RELAY OFF         - Force relay OFF (GPIO5 INPUT)");
  Out.println("RELAY AUTO        - Resume automatic relay control");
  Out.println("CONVEYOR ON       - Force conveyor to run");
  Out.println("CONVEYOR OFF      - Force conveyor to stop");
  Out.println("CONVEYOR AUTO     - Resume 2-second RUN / 5-second STOP cycle");
  Out.println("CONVEYOR SPEED n  - Run at n percent (1-100); 0 = stop");
  Out.println("LED GREEN/RED/BOTH/OFF - Force the LEDs (manual)");
  Out.println("LED AUTO          - LEDs follow the energy logic again");
  Out.println("HELP              - Show this command list");
  Out.println("Use 115200 baud and select Newline as line ending.");
  Out.println("=====================================\n");
}


void processCommand(char *command) {
  for (uint8_t i = 0; command[i] != '\0'; i++) {
    command[i] = (char)toupper((unsigned char)command[i]);
  }

  while (*command == ' ') {
    command++;
  }

  if (strcmp(command, "HELP") == 0) {
    printHelp();
  }
  else if (strcmp(command, "STATUS") == 0) {
    printSystemStatus();
  }
  else if (strcmp(command, "RELAY ON") == 0) {
    relayAutoMode = false;
    setRelay(true);
    Out.println("Manual relay command accepted.");
    printRelayState();
  }
  else if (strcmp(command, "RELAY OFF") == 0) {
    relayAutoMode = false;
    setRelay(false);
    Out.println("Manual relay command accepted.");
    printRelayState();
  }
  else if (strcmp(command, "RELAY AUTO") == 0) {
    relayAutoMode = true;
    lastRelayToggle = millis();
    Out.println(SOLAR_CONTROLS_RELAY
                  ? "Relay automatic mode resumed. It follows the charging permission."
                  : "Relay automatic mode resumed. It will toggle every 3 seconds.");
    printRelayState();
  }
  else if (strcmp(command, "CONVEYOR ON") == 0) {
    conveyorAutoMode = false;
    setConveyorState(true);
    Out.println("Manual conveyor command: RUNNING.");
  }
  else if (strcmp(command, "CONVEYOR OFF") == 0) {
    conveyorAutoMode = false;
    setConveyorState(false);
    Out.println("Manual conveyor command: STOPPED.");
  }
  else if (strcmp(command, "CONVEYOR AUTO") == 0) {
    conveyorAutoMode = true;
    setConveyorState(true);
    lastConveyorChange = millis();
    Out.println("Conveyor automatic mode resumed: 2 seconds RUN / 5 seconds STOP.");
  }
  else if (strncmp(command, "CONVEYOR SPEED", 14) == 0) {
    char *arg = command + 14;
    while (*arg == ' ') {
      arg++;
    }

    if (*arg < '0' || *arg > '9') {
      Out.println("Usage: CONVEYOR SPEED n   (n = 0 to 100)");
    } else {
      int pct = atoi(arg);
      if (pct > 100) {
        pct = 100;
      }

      conveyorAutoMode = false;
      if (pct == 0) {
        setConveyorState(false);
        Out.println("Manual conveyor command: STOPPED.");
      } else {
        conveyorSpeedPct = pct;
        setConveyorState(true);
        Out.println("Manual conveyor command: RUNNING.");
      }

      Out.print("Conveyor speed: ");
      Out.print(conveyorSpeedPct);
      Out.println("%");
    }
  }
  else if (strcmp(command, "LED GREEN") == 0) {
    ledAutoMode = false;
    applyLeds(true, false);
    Out.println("Manual LED command accepted.");
    printLedState();
  }
  else if (strcmp(command, "LED RED") == 0) {
    ledAutoMode = false;
    applyLeds(false, true);
    Out.println("Manual LED command accepted.");
    printLedState();
  }
  else if (strcmp(command, "LED BOTH") == 0) {
    ledAutoMode = false;
    applyLeds(true, true);
    Out.println("Manual LED command accepted.");
    printLedState();
  }
  else if (strcmp(command, "LED OFF") == 0) {
    ledAutoMode = false;
    applyLeds(false, false);
    Out.println("Manual LED command accepted.");
    printLedState();
  }
  else if (strcmp(command, "LED AUTO") == 0) {
    ledAutoMode = true;
    updateSolarSensors();
    Out.println("LED automatic mode resumed.");
    printLedState();
  }
  else {
    Out.print("Unknown command: ");
    Out.println(command);
    Out.println("Type HELP to see the available commands.");
  }
}


void feedCommandChar(char incoming) {
  if (incoming == '\r') {
    return;
  }

  if (incoming == '\n') {
    commandBuffer[commandLength] = '\0';

    if (commandLength > 0) {
      processCommand(commandBuffer);
    }

    commandLength = 0;
  }
  else {
    if (commandLength < sizeof(commandBuffer) - 1) {
      commandBuffer[commandLength++] = incoming;
    } else {
      commandLength = 0;
      Out.println("Command too long. Type HELP for valid commands.");
    }
  }
}


void handleSerialCommands() {
  while (Serial.available() > 0) {
    feedCommandChar((char)Serial.read());
  }

  while (bleInTail != bleInHead) {
    char c = bleIn[bleInTail];
    bleInTail = (bleInTail + 1) % BLE_IN_SIZE;
    feedCommandChar(c);
  }
}


// =====================================================
// SETUP
// =====================================================

void setup() {
  Serial.begin(115200);
  delay(1000);
  setupBle();

  Out.println("\n========== ESP32 COMBINED ENERGY SYSTEM ==========");

  // Safe start-up: relay OFF, EV deferred (red), conveyor running.
  pinMode(RELAY_PIN, INPUT);
  relayState = false;
  relayAutoMode = true;
  Out.println("Relay initialised: OFF (GPIO5 INPUT)");

  pinMode(greenLedPin, OUTPUT);
  pinMode(redLedPin, OUTPUT);
  ledAutoMode = true;
  applyLeds(false, true);

  analogReadResolution(12);
  analogSetPinAttenuation(solarPin1, ADC_11db);
  analogSetPinAttenuation(solarPin2, ADC_11db);
  analogSetPinAttenuation(solarPin3, ADC_11db);

  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);

  Out.println("I2C initialised:");
  Out.println("SDA = GPIO18");
  Out.println("SCL = GPIO19");
  scanI2C();

  Out.println("Initialising INA219...");
  if (ina219.begin(&Wire)) {
    ina219Ready = true;
    Out.println("SUCCESS: INA219 detected at address 0x40!");
  } else {
    ina219Ready = false;
    Out.println("ERROR: INA219 not detected!");
    Out.println("Check VCC, GND, SDA, SCL and I2C address.");
  }

  conveyorServo.setPeriodHertz(50);
  conveyorServo.attach(servoPin, 500, 2500);
  conveyorAutoMode = true;
  setConveyorState(true);

  unsigned long now = millis();
  lastRelayToggle = now;
  lastConveyorChange = now;
  lastSolarRead = now;
  lastInaRead = now;
  lastStatusPrint = now;

  Out.println("\nSystem configuration:");
  Out.println("Solar panels: GPIO32, GPIO35, GPIO34");
  Out.println("Solar thresholds: low < 2.0 V, recover > 2.3 V");
  Out.println("Servo conveyor: GPIO27");
  Out.println("Green LED: GPIO22");
  Out.println("Red LED: GPIO23");
  Out.println("Relay: GPIO5 (active LOW / INPUT when OFF)");
  Out.println("INA219: SDA GPIO18, SCL GPIO19, address 0x40");
  Out.println("Conveyor automatic cycle: 2 seconds RUN / 5 seconds STOP");
  Out.println(SOLAR_CONTROLS_RELAY
                ? "Relay policy: follows the charging permission"
                : "Relay policy: demo toggle every 3 seconds (solar control OFF)");

  printHelp();
  Out.println("Bluetooth: advertising as SunShine-ESP32");
  Out.println("SYSTEM TEST STARTED\n");
}


// =====================================================
// MAIN LOOP
// =====================================================

void loop() {
  handleSerialCommands();

  unsigned long now = millis();

  updateRelay(now);
  updateConveyor(now);

  if (now - lastSolarRead >= SOLAR_INTERVAL) {
    lastSolarRead = now;
    updateSolarSensors();
  }

  if (ina219Ready && now - lastInaRead >= INA_INTERVAL) {
    lastInaRead = now;
    readINA219();
  }

  if (now - lastStatusPrint >= STATUS_INTERVAL) {
    lastStatusPrint = now;
    printSystemStatus();
  }

  bleFlush();
}
