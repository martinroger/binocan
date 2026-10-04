# Binocan DBC tools

A small Python package for working on `binocan.dbc`: a browser view of the
database, a consistency check, a busload calculator and C regeneration. It
runs anywhere Python 3.10+ runs (cantools 43 and later need 3.10) and needs only `cantools`, which it offers to
install or update when it is missing or outdated.

Run everything from this folder (`tools/dbc_editor/`); the DBC argument
defaults to `binocan.dbc` at the repository root.

```bash
python -m binocan_dbc edit        # open the DBC in your browser
python -m binocan_dbc check       # overlaps, DLC, ranges, nodes, duplicates
python -m binocan_dbc busload     # theoretical bus load
python -m binocan_dbc generate    # regenerate src/binocan.c and src/binocan.h
python -m binocan_dbc doctor      # Python, pip and cantools versions
```

Or install it into your environment with `pip install -e tools/dbc_editor`
and use the `binocan-dbc` command instead of `python -m binocan_dbc`.

## cantools

Before any command, the tool checks the cantools installed for the Python that
runs it:

- not installed: asks to run `pip install "cantools>=43.0.2"`;
- older than 43.0.2 (the version that produced the committed C files): asks to upgrade;
- older than the latest on PyPI: offers the update (skipped silently when offline).

`--yes` answers those prompts for scripts, `--no-update-check` skips the PyPI query.

## Browser view

`edit` starts a server on `127.0.0.1` and opens a link carrying a random
session token; nothing else on the machine or in the browser can call it.
The view has:

- **Messages**: properties, comment, signal table and the 8 x 8 bit layout
  grid (MSB and LSB marked, overlaps outlined, a selector for multiplexer values);
- **Nodes**: who sends and who receives each message;
- **Value tables**: global `VAL_TABLE_`s and the signals using them;
- **Busload**: total and per-message load, with what-if cycle times and bitrate;
- **Check**: the consistency issues;
- **Generate C**: writes `src/` only when the code actually changed.

Editing from the browser comes in the next milestone. The view reloads the
file when it changes on disk, so edits made elsewhere show after **Reload**.

## Busload

Uses the same frame-size formula as vxGauge's `decoder/common/busload.py`
(47 + 8 x DLC bits for 11-bit IDs, 67 + 8 x DLC for 29-bit, 20% average bit
stuffing). Messages whose `GenMsgSendType` is `SendOnRequest` are left out,
so request-only frames such as the UDS channels never count, whatever their ID.

```bash
python -m binocan_dbc busload --baud 250000 -o 0x100:20 0x300:50
```

## Saving and fidelity

Saving (used by the editor from the next milestone) goes through
`binocan_dbc.model.save`, which:

- writes signals in a fixed order so repeated saves give identical text;
- restores attribute definitions cantools drops on output (`Baudrate`);
- re-reads the written file and refuses the save if it differs from the model;
- refuses if the file changed on disk since it was opened, and keeps a `.bak`.

## Tests

```bash
cd tools/dbc_editor
python -m unittest discover -s tests
```
