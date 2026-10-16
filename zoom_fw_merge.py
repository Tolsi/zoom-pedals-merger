#!/usr/bin/env python3
"""
Zoom MultiStomp FS.bin firmware tool — list, diff, merge, and remove effects.

Based on reverse-engineering the ZoomFirmwareEditor.jar (v0.98) bytecode.

All Zoom MultiStomp pedals (MS-50G, MS-60B, MS-70CDR, G1on, B1on) share
the same hardware and ZDL effect format. This tool lets you move effects
between firmware images — e.g. add overdrive/amp effects from MS-50G
into an MS-70CDR.

FS.bin filesystem layout:
  Header: starts with 55 AA 00 01 04 00, blocks_count at offset +8 (2B LE)
  System area: blocks 0–10 (0x0000–0xAFFF, 45056 bytes)
  Data area: from block 10 (offset 0xA000), each block = 4096 bytes

  File tables: 4 slots at 0x3000, 0x5000, 0x7000, 0x9000
    Header (8 bytes): byte[1]=0xA5 marker, byte[4]=0xFF means active
    Entries (32 bytes each):
      0–1:   start data block (LE)
      2–3:   01 FF marker
      4–7:   file size (LE)
      8–19:  filename (ASCII, null-terminated)
      20–31: FF padding
    Table can extend past 0xA000 into data blocks.

  Data blocks (4096 bytes):
    0–1: prev block (FFFF=first)
    2–3: next block (FFFF=last)
    4–5: data size in this block (max 4090)
    6–4095: content, FF-padded
"""

import struct
import math
import argparse
import sys

BLOCK_SIZE = 4096
DATA_PER_BLOCK = 4090
ENTRY_SIZE = 32
DATA_OFFSET = 0xA000
TABLE_OFFSETS = [0x3000, 0x5000, 0x7000, 0x9000]
TABLE_HEADER_SIZE = 8


class ZoomFirmware:
    def __init__(self, filepath):
        with open(filepath, 'rb') as f:
            self.raw = bytearray(f.read())

        self.blocks_count = struct.unpack_from('<H', self.raw, 8)[0]
        self.total_data_blocks = self.blocks_count - 10

        self._find_active_table()
        self._parse_entries()
        self._build_block_map()

    def _find_active_table(self):
        self.active_table_offset = None
        for offset in TABLE_OFFSETS:
            if offset + TABLE_HEADER_SIZE > len(self.raw):
                continue
            if self.raw[offset + 1] == 0xA5 and self.raw[offset + 4] == 0xFF:
                self.active_table_offset = offset
                break
        if self.active_table_offset is None:
            raise ValueError("No active file table found")

    def _parse_entries(self):
        self.patches = []
        offset = self.active_table_offset + TABLE_HEADER_SIZE

        while offset + ENTRY_SIZE <= len(self.raw):
            entry = self.raw[offset:offset + ENTRY_SIZE]

            if entry[2] != 0x01 or entry[3] != 0xFF:
                break

            start_block = struct.unpack_from('<H', entry, 0)[0]
            file_size = struct.unpack_from('<I', entry, 4)[0]

            name_bytes = entry[8:20]
            null_pos = name_bytes.find(b'\x00')
            if null_pos >= 0:
                name_bytes = name_bytes[:null_pos]
            while name_bytes and name_bytes[-1:] == b'\xff':
                name_bytes = name_bytes[:-1]
            filename = name_bytes.decode('ascii', errors='replace')

            self.patches.append({
                'filename': filename,
                'start_block': start_block,
                'size': file_size,
                'entry_offset': offset,
                'entry_bytes': bytes(entry),
            })
            offset += ENTRY_SIZE

        self.table_entries_end = offset

    def _build_block_map(self):
        self.block_owner = [None] * self.total_data_blocks

        table_end = self.table_entries_end
        if table_end > DATA_OFFSET:
            table_blocks_in_data = (table_end - DATA_OFFSET + BLOCK_SIZE - 1) // BLOCK_SIZE
            for i in range(table_blocks_in_data):
                self.block_owner[i] = '__FILE_TABLE__'

        for p in self.patches:
            current = p['start_block']
            visited = set()
            while current != 0xFFFF and current < self.total_data_blocks:
                if current in visited:
                    break
                visited.add(current)
                self.block_owner[current] = p['filename']
                blk_off = DATA_OFFSET + current * BLOCK_SIZE
                if blk_off + BLOCK_SIZE > len(self.raw):
                    break
                current = struct.unpack_from('<H', self.raw, blk_off + 2)[0]

    def read_file_content(self, patch):
        result = bytearray()
        current = patch['start_block']
        remaining = patch['size']
        visited = set()

        while current != 0xFFFF and remaining > 0 and current < self.total_data_blocks:
            if current in visited:
                break
            visited.add(current)
            blk_off = DATA_OFFSET + current * BLOCK_SIZE
            if blk_off + BLOCK_SIZE > len(self.raw):
                break
            data_size = struct.unpack_from('<H', self.raw, blk_off + 4)[0]
            chunk = min(data_size, remaining)
            result.extend(self.raw[blk_off + 6:blk_off + 6 + chunk])
            remaining -= chunk
            current = struct.unpack_from('<H', self.raw, blk_off + 2)[0]

        return bytes(result)

    def get_free_blocks(self):
        return [i for i in range(self.total_data_blocks) if self.block_owner[i] is None]

    def _get_block_chain(self, start_block):
        chain = []
        current = start_block
        visited = set()
        while current != 0xFFFF and current < self.total_data_blocks:
            if current in visited:
                break
            visited.add(current)
            chain.append(current)
            blk_off = DATA_OFFSET + current * BLOCK_SIZE
            if blk_off + BLOCK_SIZE > len(self.raw):
                break
            current = struct.unpack_from('<H', self.raw, blk_off + 2)[0]
        return chain

    def reserve_table_blocks(self, extra_entries=0):
        """Reserve data blocks the file table will need after adding entries.

        Must be called BEFORE injecting patches, with the total number of
        patches to add, so blocks are protected before any data lands on them.
        """
        future_end = self.table_entries_end + max(1, extra_entries) * ENTRY_SIZE
        if future_end > DATA_OFFSET:
            needed_blocks = (future_end - DATA_OFFSET + BLOCK_SIZE - 1) // BLOCK_SIZE
            for i in range(needed_blocks):
                if self.block_owner[i] is None:
                    self.block_owner[i] = '__FILE_TABLE__'
                    blk_off = DATA_OFFSET + i * BLOCK_SIZE
                    self.raw[blk_off:blk_off + 6] = b'\xFF' * 6

    def remove_patch(self, filename):
        idx = None
        for i, p in enumerate(self.patches):
            if p['filename'] == filename:
                idx = i
                break
        if idx is None:
            raise ValueError(f"{filename} not found")

        patch = self.patches[idx]
        chain = self._get_block_chain(patch['start_block'])

        # Wipe data blocks
        for blk_num in chain:
            blk_off = DATA_OFFSET + blk_num * BLOCK_SIZE
            self.raw[blk_off:blk_off + BLOCK_SIZE] = b'\xFF' * BLOCK_SIZE
            self.block_owner[blk_num] = None

        # Remove entry from file table by shifting subsequent entries up
        entry_off = patch['entry_offset']
        remaining_size = self.table_entries_end - (entry_off + ENTRY_SIZE)
        if remaining_size > 0:
            self.raw[entry_off:entry_off + remaining_size] = \
                self.raw[entry_off + ENTRY_SIZE:entry_off + ENTRY_SIZE + remaining_size]
        # Clear last entry slot
        self.raw[self.table_entries_end - ENTRY_SIZE:self.table_entries_end] = b'\xFF' * ENTRY_SIZE
        self.table_entries_end -= ENTRY_SIZE

        # Re-parse to rebuild offsets
        self.patches = []
        self._parse_entries()
        self._build_block_map()

        return len(chain)

    def inject_patch(self, filename, content, src_entry_bytes):
        blocks_needed = max(1, math.ceil(len(content) / DATA_PER_BLOCK))
        free = self.get_free_blocks()

        if len(free) < blocks_needed:
            raise ValueError(f"Need {blocks_needed} blocks, only {len(free)} free")
        if any(p['filename'] == filename for p in self.patches):
            raise ValueError(f"{filename} already exists")

        allocated = free[:blocks_needed]

        entry = bytearray(src_entry_bytes)
        struct.pack_into('<H', entry, 0, allocated[0])

        for i, block_num in enumerate(allocated):
            blk = bytearray(b'\xFF' * BLOCK_SIZE)
            struct.pack_into('<H', blk, 0, 0xFFFF if i == 0 else allocated[i - 1])
            struct.pack_into('<H', blk, 2, 0xFFFF if i == blocks_needed - 1 else allocated[i + 1])

            data_start = i * DATA_PER_BLOCK
            if i == blocks_needed - 1:
                chunk_size = len(content) - data_start
            else:
                chunk_size = DATA_PER_BLOCK
            struct.pack_into('<H', blk, 4, chunk_size)
            blk[6:6 + chunk_size] = content[data_start:data_start + chunk_size]

            offset = DATA_OFFSET + block_num * BLOCK_SIZE
            self.raw[offset:offset + BLOCK_SIZE] = blk
            self.block_owner[block_num] = filename

        self.raw[self.table_entries_end:self.table_entries_end + ENTRY_SIZE] = entry
        self.patches.append({
            'filename': filename,
            'start_block': allocated[0],
            'size': len(content),
            'entry_offset': self.table_entries_end,
            'entry_bytes': bytes(entry),
        })
        self.table_entries_end += ENTRY_SIZE

        return blocks_needed

    def save(self, filepath):
        with open(filepath, 'wb') as f:
            f.write(self.raw)

    @staticmethod
    def extract_effect_name(content):
        """Extract human-readable effect name from ZDL file content.

        ZDL format stores the display name as 12 ASCII bytes immediately
        before a 4-byte 0xFFFFFFFF marker, located after the "OnOff" string.
        """
        pos = content.find(b'OnOff')
        if pos == -1:
            return None
        ff4 = content.find(b'\xff\xff\xff\xff', pos)
        if ff4 == -1 or ff4 < 12:
            return None
        name_bytes = content[ff4 - 12:ff4]
        return name_bytes.decode('ascii', errors='replace').strip().rstrip('\x00')


def blocks_for(size):
    return max(1, math.ceil(size / DATA_PER_BLOCK))


def cmd_list(args):
    fw = ZoomFirmware(args.firmware)
    free = fw.get_free_blocks()
    zdl = [p for p in fw.patches if p['filename'].endswith('.ZDL')]
    print(f"\n{'=' * 70}")
    print(f" {args.firmware}: {len(fw.patches)} patches ({len(zdl)} ZDL), "
          f"{len(free)}/{fw.total_data_blocks} blocks free")
    print(f" Active table @0x{fw.active_table_offset:04X}")
    print(f"{'=' * 70}")

    show_names = getattr(args, 'names', False)
    for p in sorted(fw.patches, key=lambda x: x['filename']):
        blk = blocks_for(p['size'])
        line = f"  {p['filename']:16s}  {p['size']:>6d} bytes  ({blk:2d} blk)"
        if show_names:
            content = fw.read_file_content(p)
            name = ZoomFirmware.extract_effect_name(content)
            if name:
                line += f"  [{name}]"
        print(line)


def cmd_diff(args):
    fw_target = ZoomFirmware(args.target)
    fw_source = ZoomFirmware(args.source)

    n_target = {p['filename'] for p in fw_target.patches}
    n_source = {p['filename'] for p in fw_source.patches}
    only_source = sorted(n_source - n_target)
    only_target = sorted(n_target - n_source)

    total_blocks = sum(
        blocks_for(next(x for x in fw_source.patches if x['filename'] == n)['size'])
        for n in only_source
    )
    free_target = len(fw_target.get_free_blocks())

    show_names = getattr(args, 'names', False)

    print(f"\nTarget: {len(fw_target.patches)} patches, {free_target} free blocks")
    print(f"Source: {len(fw_source.patches)} patches")
    print(f"\nTo add from source: {len(only_source)} effects ({total_blocks} blocks needed)")
    print(f"Free in target: {free_target} blocks")
    print(f"Fits: {'YES' if free_target >= total_blocks else 'NO — need to remove effects first'}")

    print(f"\n--- Only in source ({len(only_source)}) ---")
    for n in only_source:
        p = next(x for x in fw_source.patches if x['filename'] == n)
        b = blocks_for(p['size'])
        line = f"  {n:16s}  {p['size']:>6d} bytes  ({b} blk)"
        if show_names:
            content = fw_source.read_file_content(p)
            name = ZoomFirmware.extract_effect_name(content)
            if name:
                line += f"  [{name}]"
        print(line)

    print(f"\n--- Only in target ({len(only_target)}) ---")
    for n in only_target:
        p = next(x for x in fw_target.patches if x['filename'] == n)
        b = blocks_for(p['size'])
        line = f"  {n:16s}  {p['size']:>6d} bytes  ({b} blk)"
        if show_names:
            content = fw_target.read_file_content(p)
            name = ZoomFirmware.extract_effect_name(content)
            if name:
                line += f"  [{name}]"
        print(line)


def cmd_remove(args):
    fw = ZoomFirmware(args.firmware)
    to_remove = args.effects

    print(f"Before: {len(fw.patches)} patches, {len(fw.get_free_blocks())} free blocks\n")

    freed_total = 0
    for name in to_remove:
        try:
            freed = fw.remove_patch(name)
            freed_total += freed
            print(f"  - {name:16s}  freed {freed} blocks")
        except ValueError as e:
            print(f"  SKIP {name}: {e}")

    fw.save(args.output)

    verify = ZoomFirmware(args.output)
    print(f"\n{'=' * 60}")
    print(f" DONE: {len(verify.patches)} patches ({freed_total} blocks freed)")
    print(f" Free blocks: {len(verify.get_free_blocks())}")
    print(f" Saved: {args.output}")
    print(f"{'=' * 60}")


def cmd_merge(args):
    fw_target = ZoomFirmware(args.target)
    fw_source = ZoomFirmware(args.source)

    n_target = {p['filename'] for p in fw_target.patches}
    n_source = {p['filename'] for p in fw_source.patches}

    if args.effects:
        to_add = [e for e in args.effects if e in n_source and e not in n_target]
        skipped = [e for e in args.effects if e not in n_source]
        if skipped:
            print(f"Warning: not in source: {', '.join(skipped)}")
        dupes = [e for e in args.effects if e in n_target]
        if dupes:
            print(f"Warning: already in target: {', '.join(dupes)}")
    else:
        to_add = sorted(n_source - n_target)

    if not to_add:
        print("Nothing to add!")
        return

    print(f"Merging {len(to_add)} effects into target...")
    print(f"Before: {len(fw_target.patches)} patches, "
          f"{len(fw_target.get_free_blocks())} free blocks\n")

    fw_target.reserve_table_blocks(len(to_add))

    added = 0
    for name in to_add:
        src = next(x for x in fw_source.patches if x['filename'] == name)
        content = fw_source.read_file_content(src)

        if len(content) != src['size']:
            print(f"  SKIP {name}: read {len(content)}/{src['size']} bytes")
            continue

        try:
            blk = fw_target.inject_patch(name, content, src['entry_bytes'])
            print(f"  + {name:16s}  {src['size']:>6d} bytes  ({blk} blk)")
            added += 1
        except ValueError as e:
            print(f"  FAIL {name}: {e}")
            break

    fw_target.save(args.output)

    verify = ZoomFirmware(args.output)
    print(f"\n{'=' * 60}")
    print(f" DONE: {len(verify.patches)} patches ({added} added)")
    print(f" Free blocks: {len(verify.get_free_blocks())}")
    print(f" Saved: {args.output}")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(
        description='Zoom MultiStomp firmware effect merger/editor.\n'
                    'Manages ZDL effects in FS.bin firmware images for '
                    'MS-50G, MS-60B, MS-70CDR, G1on, B1on.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  %(prog)s list firmware/FS.bin
  %(prog)s list firmware/FS.bin --names
  %(prog)s diff --target ms70cdr/FS.bin --source ms50g/FS.bin --names
  %(prog)s merge --target ms70cdr/FS.bin --source ms50g/FS.bin -o merged.bin
  %(prog)s merge --target ms70cdr/FS.bin --source ms50g/FS.bin -o merged.bin --effects OVRDRV.ZDL LEAD.ZDL
  %(prog)s remove firmware/FS.bin -o trimmed.bin --effects B_CHORUS.ZDL B_FLNGR.ZDL B_GEQ.ZDL"""
    )
    sub = parser.add_subparsers(dest='command')

    p_list = sub.add_parser('list', help='List all effects in a firmware')
    p_list.add_argument('firmware', help='FS.bin path')
    p_list.add_argument('--names', action='store_true',
                        help='Show human-readable effect names (slower — reads ZDL content)')

    p_diff = sub.add_parser('diff', help='Compare two firmwares, show unique effects')
    p_diff.add_argument('--target', required=True, help='Target FS.bin (e.g. MS-70CDR)')
    p_diff.add_argument('--source', required=True, help='Source FS.bin (e.g. MS-50G)')
    p_diff.add_argument('--names', action='store_true',
                        help='Show human-readable effect names')

    p_merge = sub.add_parser('merge', help='Add effects from source into target firmware')
    p_merge.add_argument('--target', required=True, help='Target FS.bin path')
    p_merge.add_argument('--source', required=True, help='Source FS.bin (effects donor)')
    p_merge.add_argument('-o', '--output', required=True, help='Output FS.bin path')
    p_merge.add_argument('--effects', nargs='+', metavar='FILE.ZDL',
                         help='Only add these specific effects (default: all missing)')

    p_remove = sub.add_parser('remove', help='Remove effects from firmware to free space')
    p_remove.add_argument('firmware', help='FS.bin path')
    p_remove.add_argument('-o', '--output', required=True, help='Output FS.bin path')
    p_remove.add_argument('--effects', nargs='+', required=True, metavar='FILE.ZDL',
                          help='Effects to remove')

    args = parser.parse_args()
    if args.command == 'list':
        cmd_list(args)
    elif args.command == 'diff':
        cmd_diff(args)
    elif args.command == 'merge':
        cmd_merge(args)
    elif args.command == 'remove':
        cmd_remove(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
