# Binocan CAN Protocol — Theory of Operation

## 1. Protocol Architecture & DBC Definition

The **Binocan** protocol defines all CAN bus communications between the Interface Board (ITF), Left Display Board (LDB), Right Display Board (RDB), and diagnostic tools. The canonical network database is maintained in [`binocan.dbc`](binocan.dbc).

```mermaid
graph LR
    A[Interface Board (ITF)] -->|0x100: ITF_values| B(Left Screen - LDB)
    A -->|0x101: ITF_status| B
    A -->|0x100: ITF_values| C(Right Screen - RDB)
    A -->|0x101: ITF_status| C
    A -->|0x102: ITF_debug| D(Diagnostic Logger / PC)
```

---

## 2. Message Dictionary & Timing

| Message Name | CAN ID (Hex) | Periodicity | Key Signals |
| :--- | :--- | :--- | :--- |
| **`ITF_values`** | `0x100` | 20 ms | Speed (km/h), Engine RPM, Odometer pulse delta |
| **`ITF_status`** | `0x101` | 50 ms | Coolant Temperature (°C), Fuel level (L), Battery Voltage (V), Indicator bitmask |
| **`ITF_debug`** | `0x102` | 100 ms | Raw ADC channels 0–3, SMA filtered voltages, internal loop execution stats |

---

## 3. C Binding Generation

The generated C bindings (`binocan.c` and `binocan.h`) provide pack/unpack routines with integer scaling and endianness normalization:
- `binocan_itf_values_pack()` / `binocan_itf_values_unpack()`
- `binocan_itf_status_pack()` / `binocan_itf_status_unpack()`
- `binocan_itf_debug_pack()` / `binocan_itf_debug_unpack()`
