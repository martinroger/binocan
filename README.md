# Binocan ESP-IDF Component

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An ESP-IDF component providing C bindings and DBC definitions for the **Binocan CAN protocol**. It defines communications between the Interface Board (ITF), Left Display Board (LDB), Right Display Board (RDB), and diagnostic tools.

---

## Features

- **Generated C Bindings**: Pack and unpack routines with integer scaling and endianness normalization (`binocan.h` / `binocan.c`).
- **DBC Definitions**:
  - `binocan.dbc`: Canonical network database.
  - `binocan_savvy.dbc`: SavvyCAN-friendly definitions.
  - `racebox_companion.dbc`: Companion telemetry DBC.
- **ESP-IDF Compatible**: Ready to be consumed as an ESP-IDF component (`idf_component.yml` included).

---

## Requirements

- **ESP-IDF**: `>= 5.5.5`
- **Supported Targets**: All ESP-IDF targets (`esp32`, `esp32s2`, `esp32s3`, `esp32c3`, `esp32c6`, etc.)

---

## Usage

### 1. Add as a Dependency

In your project or component's `idf_component.yml`:

```yaml
dependencies:
  binocan:
    git: https://github.com/martinroger/binocan.git
    # Or reference local directory:
    # path: /path/to/binocan
```

Or clone it directly into your project's `components/` directory:

```bash
cd your_project/components
git clone https://github.com/martinroger/binocan.git
```

### 2. Include Header in C/C++ Code

```c
#include "binocan.h"

// Example: Packing ITF status message
struct binocan_itf_status_t status = {
    .coolant_temp = 85,
    .fuel_level = 42,
    .battery_voltage = 13.8f,
    .indicators = 0x01,
};

uint8_t payload[8];
int len = binocan_itf_status_pack(payload, &status, sizeof(payload));

// Transmit payload over TWAI / CAN driver...
```

---

## CAN Message Dictionary & Timing

| Message Name | CAN ID (Hex) | Periodicity | Key Signals |
| :--- | :--- | :--- | :--- |
| **`ITF_values`** | `0x100` | 20 ms | Speed (km/h), Engine RPM, Odometer pulse delta |
| **`ITF_status`** | `0x101` | 50 ms | Coolant Temperature (°C), Fuel level (L), Battery Voltage (V), Indicator bitmask |
| **`ITF_debug`** | `0x102` | 100 ms | Raw ADC channels 0–3, SMA filtered voltages, internal loop stats |

For architectural details, see [TOO.MD](TOO.MD).

---

## License

This project is licensed under the [MIT License](LICENSE).
