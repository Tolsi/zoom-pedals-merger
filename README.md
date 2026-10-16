# Zoom MultiStomp Firmware Effect Merger

Merge ZDL effects between Zoom MultiStomp pedal firmwares. All models (MS-50G, MS-60B, MS-70CDR, G1on, B1on) share the same hardware — only the software limits which effects are available. This tool removes that limitation.

**Typical use case:** Add overdrive/amp/distortion effects from MS-50G into MS-70CDR (which only ships with chorus/delay/reverb).

## Requirements

- Python 3.6+
- No external dependencies

## Getting the firmware files

1. Download official firmware updaters from [Zoom](https://zoomcorp.com/en/jp/support/):
   - [MS-70CDR v2.10](https://zoomcorp.com/en/jp/multi-effects/multistomp/ms-70cdr/ms-70cdr-support/) (Mac or Windows)
   - [MS-50G v3.10](https://zoomcorp.com/en/jp/multi-effects/multistomp/ms-50g/ms-50g-support/) (Mac or Windows)
2. Extract the updater archive
3. Locate `FS.bin`:
   - **Mac:** `ZOOM MS-xxxx vX.XX Updater.app/Contents/Resources/FS.bin`
   - **Windows:** `ZOOM MS-xxxx vX.XX Updater/FS.bin`

## Usage

### List effects in a firmware

```bash
python3 zoom_fw_merge.py list path/to/FS.bin
python3 zoom_fw_merge.py list path/to/FS.bin --names  # show human-readable names
```

### Compare two firmwares

```bash
python3 zoom_fw_merge.py diff --target ms70cdr/FS.bin --source ms50g/FS.bin --names
```

Shows effects unique to each firmware and whether there's enough space to merge.

### Merge effects

```bash
# Add ALL missing effects from source into target
python3 zoom_fw_merge.py merge --target ms70cdr/FS.bin --source ms50g/FS.bin -o merged_FS.bin

# Add specific effects only
python3 zoom_fw_merge.py merge --target ms70cdr/FS.bin --source ms50g/FS.bin -o merged_FS.bin \
  --effects OVRDRV.ZDL LEAD.ZDL DISTONE.ZDL
```

### Remove effects (free space for more imports)

The MS-70CDR firmware only has ~17 free blocks. To fit all 30 MS-50G effects (~101 blocks needed), remove effects you don't use first:

```bash
# Remove bass-specific effects (useless on guitar)
python3 zoom_fw_merge.py remove ms70cdr/FS.bin -o trimmed_FS.bin \
  --effects B_CHORUS.ZDL B_FLNGR.ZDL B_GEQ.ZDL B_DETUNE.ZDL B_ENSMBL.ZDL \
  B_OCTAVE.ZDL B_PEQ.ZDL B_PITCH.ZDL B_CRY.ZDL B_ATWAH.ZDL BOTTOM_B.ZDL ST_B_GEQ.ZDL

# Then merge
python3 zoom_fw_merge.py merge --target trimmed_FS.bin --source ms50g/FS.bin -o final_FS.bin
```

## Flashing the modified firmware

1. Place the modified `FS.bin` back into the updater:
   - **Mac:** Right-click the `.app` → "Show Package Contents" → `Contents/Resources/FS.bin`
   - **Windows:** Replace `FS.bin` in the updater folder
2. Connect the pedal in update mode:
   - Hold **both arrow buttons** on the pedal, then connect USB
   - Display shows `ZOOM MS-70CDR` (or similar)
3. Run the updater application
4. Select `ZOOM MS Series` for both MIDI IN and MIDI OUT
5. Click "Execute"

## MS-50G effects available for MS-70CDR

| ZDL File | Effect Name | Category |
|----------|------------|----------|
| OVRDRV.ZDL | OverDrive | Drive |
| DISTONE.ZDL | Dist 1 | Drive |
| DISTPLUS.ZDL | Dist+ | Drive |
| LEAD.ZDL | Lead | Drive |
| BOOSTER.ZDL | Booster | Drive |
| HOTBOX.ZDL | HotBox | Drive |
| T_SCREAM.ZDL | T Scream | Drive |
| SQUEAK.ZDL | Squeak | Drive |
| GOVERNOR.ZDL | Governor | Drive |
| GRTMUFF.ZDL | GreatMuff | Drive |
| FUZZSML.ZDL | FuzzSmile | Drive |
| EXTRMDS.ZDL | ExtremeDS | Drive |
| MTLWRLD.ZDL | MetalWRLD | Drive |
| ACOSIM.ZDL | Aco.Sim | Drive |
| ALIEN.ZDL | ALIEN | Drive |
| Z_CLEAN.ZDL | Z Clean | Amp |
| Z_DREAM.ZDL | Z Dream | Amp |
| Z_SCREAM.ZDL | Z Scream | Amp |
| Z_NEOS.ZDL | Z Neos | Amp |
| Z_WILD.ZDL | Z Wild | Amp |
| Z_BOTTOM.ZDL | Z Bottom | Amp |
| Z_MP1.ZDL | Z MP1 | Amp |
| MS_1959.ZDL | MS 1959 | Amp |
| DZ_DRIVE.ZDL | DZ DRIVE | Amp |
| TW_ROCK.ZDL | TW ROCK | Amp |
| FDCOMBO.ZDL | FD COMBO | Amp |
| DELUXE_R.ZDL | DELUXE-R | Amp |
| US_BLUES.ZDL | US BLUES | Amp |
| VX_JMI.ZDL | VX JMI | Amp |
| CMN_DRV.ZDL | *(shared driver)* | System |

> **Note:** `CMN_DRV.ZDL` is a shared driver required by all amp/drive effects. It is automatically included when merging.

## How it works

The tool parses the FS.bin filesystem used by Zoom MultiStomp pedals:

- 4 file table slots at fixed offsets (0x3000, 0x5000, 0x7000, 0x9000), one active
- Each table entry (32 bytes) points to a chain of 4096-byte data blocks
- Data blocks are doubly-linked (prev/next pointers) with up to 4090 bytes of payload
- Effects are stored as `.ZDL` files containing DSP code and metadata

The filesystem format was reverse-engineered from the [ZoomFirmwareEditor](https://github.com/Barsik-Barbosik/Zoom-Firmware-Editor) Java application (v0.98).

## Warnings

- **Modified firmware can brick your pedal** with no recovery path. Use at your own risk.
- Always keep a backup of the original `FS.bin`.
- This voids your warranty.
- Only tested with MS-70CDR v2.10 and MS-50G v3.10 firmwares.

## License

MIT
