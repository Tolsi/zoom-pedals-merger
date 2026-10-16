# Zoom MultiStomp Firmware Effect Merger

Merge ZDL effects between Zoom MultiStomp pedal firmwares. All models (MS-50G, MS-60B, MS-70CDR, G1on, B1on) share the same hardware — only the software limits which effects are available. This tool removes that limitation.

**Typical use case:** Add overdrive/amp/distortion effects from MS-50G into MS-70CDR (which only ships with chorus/delay/reverb).

## Requirements

- Python 3.6+
- `zoom_fw_merge.py`: no external dependencies
- `zoom_flash.py`: `pip install mido python-rtmidi`

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
  B_OCTAVE.ZDL B_PEQ.ZDL B_PITCH.ZDL B_CRY.ZDL B_ATWAH.ZDL BOTTOM_B.ZDL ST_B_GEQ.ZDL \
  MNGLD_SP.ZDL SPC_HOLE.ZDL LOFIDLY.ZDL DUAL_CMP.ZDL

# Then merge
python3 zoom_fw_merge.py merge --target trimmed_FS.bin --source ms50g/FS.bin -o final_FS.bin
```

## Flashing the modified firmware

### Python flasher (macOS / Linux / Windows)

`zoom_flash.py` talks to the pedal's bootloader directly — no official updater needed.

```bash
pip install mido python-rtmidi

# Hold both arrow buttons on the pedal while connecting USB (update mode), then:
python3 zoom_flash.py final_FS.bin --log flash.log

# Check what would be sent without touching the pedal:
python3 zoom_flash.py final_FS.bin --dry-run
```

By default only `FS.bin` (the effects filesystem) is written; the bootloader and main
firmware stay untouched. `--full DIR` reproduces the official updater exactly
(boot.bin, Preset.bin, FS.bin, Main.bin from an extracted updater's `Resources` folder).

Tested on MS-70CDR (boot 1.10): FS.bin flashed in ~40 s, all 1426 packets acknowledged.
If anything fails mid-way the pedal stays in update mode — just re-run the script
(or the official updater with the stock firmware).

#### Update-mode protocol

Reverse-engineered from the official macOS updater (`_allocateFlashErase`, `_allocateFlashWrite`, `-[MainWindow DoUpdate]`, `_RomProcedure`):

| Message | Bytes |
|---|---|
| Identity request | `F0 7E 00 06 01 F7` → `F0 7E 00 06 02 52 <dev> 00 00 00 <boot ver> F7` |
| CanUpdate | `F0 52 00 <dev> 01 F7` |
| FlashErase | `F0 52 00 <dev> 0A <addr:5×7bit LE> <size:5×7bit LE> F7` |
| FlashWrite | `F0 52 00 <dev> 02 <addr:5×7bit LE> <len:2×7bit LE> <data, 8→7 encoded> <crc32:5×7bit LE> F7` |
| PacketProcessed (finish) | `F0 52 00 <dev> 04 F7` |
| Pedal reply | `F0 52 00 <dev> 00 <status> F7`, status `00` = OK |

- Writes are 2048-byte chunks; each waits for a reply.
- 8→7 encoding: per 7 data bytes, one header byte whose bit `0x40 >> j` holds the MSB of byte `j`, then the 7 bytes with MSB cleared.
- CRC32: standard table, init `0xFFFFFFFF`, **no** final XOR, over the raw 8-bit chunk.

Flash layout (official procedure, in order):

| Step | Address | Size | Content |
|---|---|---|---|
| erase | `0x0EF000` | `0x1000` | — |
| erase + write | `0x000000` | `0x40000` | boot.bin (bootloader) |
| erase + write | `0x3FD000` | `0x3000` | Preset.bin |
| erase + write | `0x0F0000` | `0x2C7000` | FS.bin |
| erase + write | `0x040000` | `0xB0000` | Main.bin |

### Windows (official updater)

1. Replace `FS.bin` in the updater folder with your modified version
2. Hold **both arrow buttons** on the pedal, then connect USB
3. Run the updater, select `ZOOM MS Series` for MIDI IN/OUT, click "Execute"

### macOS (official updater on modern versions — Sequoia, Sonoma, etc.)

The official Mac updater (built for macOS 10.14) crashes on modern macOS due to a removed `setTextColor:` API on `_NSAlertButton`. It also validates `FS.bin` via MD5 hash embedded in the binary. Two patches are needed:

#### Step 1: Patch the MD5 hash

The updater binary contains MD5 hashes for all resource files. After replacing `FS.bin`, the original hash must be updated:

```bash
# Compute MD5 of your modified FS.bin
MODDED_MD5=$(md5 -q path/to/your/modified_FS.bin)

# Find and replace the original MD5 in the updater binary
# Original MS-70CDR v2.10 FS.bin MD5: e047c323b3f2355137ba79ab4e558a01
# The hash is stored as a 32-char ASCII hex string at offset 0x206CD in the binary

BINARY="ZOOM MS-70CDR v2.10 Updater.app/Contents/MacOS/EFX Updater"
python3 -c "
data = bytearray(open('$BINARY', 'rb').read())
old = b'e047c323b3f2355137ba79ab4e558a01'
new = b'$MODDED_MD5'
pos = data.find(old)
if pos < 0:
    print('Original MD5 not found — binary may already be patched')
else:
    data[pos:pos+32] = new
    open('$BINARY', 'wb').write(data)
    print(f'Patched MD5 at offset 0x{pos:X}')
"
```

#### Step 2: Fix the macOS crash

Compile a small dylib that adds the missing method:

```bash
cat > /tmp/fix_alert.m << 'EOF'
#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>
#import <objc/runtime.h>

__attribute__((constructor))
static void fix_alert_button(void) {
    Class cls = NSClassFromString(@"_NSAlertButton");
    if (cls && !class_getInstanceMethod(cls, @selector(setTextColor:))) {
        class_addMethod(cls, @selector(setTextColor:),
            imp_implementationWithBlock(^(id self, id color) {}),
            "v@:@");
    }
}
EOF

clang -dynamiclib -framework Foundation -framework AppKit \
  -arch x86_64 -o /tmp/fix_alert.dylib /tmp/fix_alert.m
```

#### Step 3: Re-sign and launch

```bash
# Replace FS.bin
cp path/to/modified_FS.bin "ZOOM MS-70CDR v2.10 Updater.app/Contents/Resources/FS.bin"

# Re-sign (required after any binary modification)
codesign --force --deep --sign - "ZOOM MS-70CDR v2.10 Updater.app"

# Connect pedal in update mode (hold both arrow buttons + USB)
# Launch with the fix
DYLD_INSERT_LIBRARIES=/tmp/fix_alert.dylib \
  "ZOOM MS-70CDR v2.10 Updater.app/Contents/MacOS/EFX Updater"
```

Select `ZOOM MS Series` for MIDI IN and MIDI OUT, click "Execute".

## Effects unique to MS-50G (not in MS-70CDR)

30 effects — mostly drive, distortion, and amp simulators:

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

## Effects unique to MS-70CDR (not in MS-50G)

66 effects — reverbs, delays, choruses, bass effects, compressors:

| ZDL File | Effect Name | Category |
|----------|------------|----------|
| 160_COMP.ZDL | 160 Comp | Compressor |
| AMBIENCE.ZDL | Ambience | Reverb |
| ANA234CH.ZDL | ANA234Cho | Chorus |
| APANDLY.ZDL | A-Pan DLY | Delay |
| A_FILTER.ZDL | A-Filter | Filter |
| BF_FLG_2.ZDL | BF FLG 2 | Flanger |
| BOTTOM_B.ZDL | Bottom B | Bass |
| B_ATWAH.ZDL | BaAutoWah | Bass |
| B_CHORUS.ZDL | Ba Chorus | Bass |
| B_CRY.ZDL | Ba Cry | Bass |
| B_DETUNE.ZDL | Ba Detune | Bass |
| B_ENSMBL.ZDL | Ba Ensmbl | Bass |
| B_FLNGR.ZDL | BaFlanger | Bass |
| B_GEQ.ZDL | Ba GEQ | Bass |
| B_OCTAVE.ZDL | Ba Octave | Bass |
| B_PEQ.ZDL | Ba PEQ | Bass |
| B_PITCH.ZDL | Ba Pitch | Bass |
| CAVE.ZDL | Cave | Reverb |
| CE_CHO5.ZDL | CE-Cho5 | Chorus |
| CHAMBER.ZDL | Chamber | Reverb |
| CHURCH.ZDL | Church | Reverb |
| CLONECHO.ZDL | CloneCho | Chorus |
| CRBNDLY.ZDL | CarbonDly | Delay |
| CRN_CHO.ZDL | CoronaCho | Chorus |
| CRN_TRI.ZDL | CoronaTri | Chorus |
| DIRTYGTE.ZDL | DirtyGate | Gate |
| DRV_ECHO.ZDL | DriveEcho | Delay |
| DUALDIGD.ZDL | DualDigiD | Delay |
| DUAL_CMP.ZDL | DualComp | Compressor |
| DUAL_REV.ZDL | DualRev | Reverb |
| DUOTREM.ZDL | DuoTrem | Tremolo |
| DYNAREV.ZDL | DynaRev | Reverb |
| ECHO.ZDL | Echo | Delay |
| FLTERPPD.ZDL | FLTR PPD | Filter |
| GATE_REV.ZDL | GateRev | Reverb |
| GRAYCOMP.ZDL | GrayComp | Compressor |
| HolyFLRB.ZDL | HolyFLERB | Reverb |
| ICEDLY.ZDL | ICE Delay | Delay |
| LIMITER.ZDL | Limiter | Compressor |
| LOFIDLY.ZDL | LO-FI Dly | Delay |
| LOFI_REV.ZDL | LOFI Rev | Reverb |
| MIRAGECH.ZDL | MirageCho | Chorus |
| MNGLD_SP.ZDL | MangledSp | Reverb |
| MODDLY2.ZDL | ModDelay2 | Delay |
| MODREV.ZDL | ModReverb | Reverb |
| ORANGELM.ZDL | OrangeLim | Compressor |
| PARTICLE.ZDL | ParticleR | Reverb |
| PLATE.ZDL | Plate | Reverb |
| RVS_REV.ZDL | ReverseRv | Reverb |
| SHIMMER.ZDL | ShimmerRv | Reverb |
| SILKYCHO.ZDL | SilkyCho | Chorus |
| SLAPBACK.ZDL | SlapBack | Delay |
| SLAPBKD.ZDL | SlapBackD | Delay |
| SLOWFLTR.ZDL | SlowFLTR | Filter |
| SLWATKDL.ZDL | SlwAtkDly | Delay |
| SMS_DLY.ZDL | SmoothDly | Delay |
| SPC_HOLE.ZDL | SpaceHole | Reverb |
| SPLITTER.ZDL | Splitter | Utility |
| SPRING63.ZDL | Spring63 | Reverb |
| STONEPHA.ZDL | StonePha | Phaser |
| ST_B_GEQ.ZDL | St Bs GEQ | Bass |
| ST_G_GEQ.ZDL | St Gt GEQ | EQ |
| TAPEECH3.ZDL | TapeEcho3 | Delay |
| TREMDLY.ZDL | TremDelay | Delay |
| TREM_REV.ZDL | TremoloRv | Reverb |
| Z_TRON.ZDL | Z Tron | Filter |

## How it works

The tool parses the FS.bin filesystem used by Zoom MultiStomp pedals:

- 4 file table slots at fixed offsets (0x3000, 0x5000, 0x7000, 0x9000), one active
- Each table entry (32 bytes) points to a chain of 4096-byte data blocks
- Data blocks are doubly-linked (prev/next pointers) with up to 4090 bytes of payload
- Effects are stored as `.ZDL` files containing DSP code and metadata
- `FLST_SEQ.ZDT` is the effect menu: 13-byte records, `>>>\0<cat>` opens a category,
  `<<<\0<cat>` closes it, null-padded ZDL names in between. **An effect not listed here
  never shows up on the pedal**, even if its ZDL is present. `merge` adds new effects to
  their source category (creating DRIVE=3 / AMP=4 on MS-70CDR, which has none), `remove`
  drops them from the list. Categories: 1 DYN, 2 FILTER, 3 DRIVE, 4 AMP, 6 MOD, 7 SFX, 8 DELAY, 9 REVERB.
- The file table can grow past `0xA000` into the first data blocks. Before injecting,
  the tool reserves every block the table will need and relocates any file sitting there
  (otherwise the pedal fails to boot with `Init Err: -10205`).

The filesystem format was reverse-engineered from the [ZoomFirmwareEditor](https://github.com/Barsik-Barbosik/Zoom-Firmware-Editor) Java application (v0.98).

## Warnings

- **Modified firmware can brick your pedal** with no recovery path. Use at your own risk.
- Always keep a backup of the original `FS.bin`.
- This voids your warranty.
- Only tested with MS-70CDR v2.10 and MS-50G v3.10 firmwares.

## License

MIT
