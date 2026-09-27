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

// Example: Packing ITF slow metrics message
struct binocan_itf_slow_metrics_t metrics = {
    .itf_coolant_temp = binocan_itf_slow_metrics_itf_coolant_temp_encode(85.0),
    .itf_fuel_level_pc = binocan_itf_slow_metrics_itf_fuel_level_pc_encode(42.0),
    .itf_lv_voltage_v = binocan_itf_slow_metrics_itf_lv_voltage_v_encode(13.8),
};

uint8_t payload[8];
int len = binocan_itf_slow_metrics_pack(payload, &metrics, sizeof(payload));

// Transmit payload over TWAI / CAN driver...
```

---

## CAN Message Dictionary & Timing

The Binocan protocol defines communications for core cluster telemetry, telltales, board status, debug metrics, and external devices.

### Core Cluster & Operational Messages

| Message Name | CAN ID (Hex) | Periodicity | Key Signals |
| :--- | :--- | :--- | :--- |
| **`ITF_fast_metrics`** | `0x100` | 25 ms (40 Hz) | Speed (`ITF_speed_kph`), Engine RPM (`ITF_rpm`), Gear Position (`ITF_gear_position_ST`) |
| **`ITF_active_hi_lo`** | `0x101` | 200 ms / Event | Active telltales & inputs (turn signals, high beams, oil pressure, CEL, doors, etc.) |
| **`ITF_slow_metrics`** | `0x110` | 500 ms (2 Hz) | Coolant Temperature (`ITF_coolant_temp`), Fuel Level (`ITF_fuel_level_pc`), Low Voltage (`ITF_lv_voltage_v`) |
| **`ITF_odometer`** | `0x111` | 250 ms (4 Hz) | Total Odometer (`ITF_odometer_km` + remainder), Trip Odometer (`ITF_trip_km` + remainder) |
| **`ITF_board_ST`** | `0x120` | 200 ms (5 Hz) | Board status machine (`ITF_SM_ST`), MCU Temperature, 5V/5V AUX rail states, alive checks |
| **`LDB_ST`** | `0x121` | 200 ms (5 Hz) | Left Display Board status machine (`LDB_SM_ST`), brightness, mode lock |
| **`RDB_ST`** | `0x122` | 200 ms (5 Hz) | Right Display Board status machine (`RDB_SM_ST`), brightness, mode lock |
| **`ITF_board_version`** | `0x130` | 1000 ms (1 Hz) | Interface Board firmware version (major, minor, patch, dirty, commit ID) |
| **`LDB_board_version`** | `0x131` | 1000 ms (1 Hz) | Left Display Board firmware version |
| **`RDB_board_version`** | `0x132` | 1000 ms (1 Hz) | Right Display Board firmware version |

Additional message groups include **External Metrics** (`0x200`–`0x201`), **Diagnostic Debug** (`0x300`–`0x305`), **RaceBox Telemetry** (`0x666`–`0x66B`), and **UDS Diagnostics** (`0x780`–`0x787`). For full architectural details, signal specifications, and diagrams, see [TOO.MD](TOO.MD).

---

## License

This project is licensed under the [MIT License](LICENSE).
