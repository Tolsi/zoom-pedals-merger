#!/usr/bin/env python3
"""
Zoom MultiStomp firmware flasher (MS-50G / MS-60B / MS-70CDR) over MIDI SysEx.

Protocol reverse-engineered from the official "EFX Updater" (MS-70CDR v2.10, macOS):

  CanUpdate       F0 52 00 <dev> 01 F7
  FlashErase      F0 52 00 <dev> 0A <addr:5x7bit LE> <size:5x7bit LE> F7
  FlashWrite      F0 52 00 <dev> 02 <addr:5x7bit LE> <len:2x7bit LE> <data 8->7 encoded> <crc32:5x7bit LE> F7
  PacketProcessed F0 52 00 <dev> 04 F7
  Pedal reply     F0 52 00 <dev> 00 <status> F7   (status 00 = OK)

The updater runs this ROM procedure (addr, size, file):
  erase 0x0EF000 0x001000
  erase/write 0x000000 0x040000 boot.bin    (bootloader!)
  erase/write 0x3FD000 0x003000 Preset.bin
  erase/write 0x0F0000 0x2C7000 FS.bin      (effects filesystem)
  erase/write 0x040000 0x0B0000 Main.bin
then sends PacketProcessed. Writes go in 2048-byte chunks, each acknowledged.

By default only FS.bin is flashed (bootloader and main firmware untouched).
The pedal must be in update mode: hold both arrow buttons while connecting USB.

Requires: pip install mido python-rtmidi
"""

import argparse
import os
import sys
import time
import zlib

import mido

CHUNK = 0x800
MARKER_ERASE = (0x0EF000, 0x001000)
LAYOUT = {
    'boot.bin': (0x000000, 0x040000),
    'Preset.bin': (0x3FD000, 0x003000),
    'FS.bin': (0x0F0000, 0x2C7000),
    'Main.bin': (0x040000, 0x0B0000),
}
FULL_ORDER = ['boot.bin', 'Preset.bin', 'FS.bin', 'Main.bin']
DEVICES = {0x58: 'MS-50G', 0x5F: 'MS-60B', 0x61: 'MS-70CDR'}


def septets(value, n):
    return [(value >> (7 * i)) & 0x7F for i in range(n)]


def convert8to7(data):
    out = []
    for i in range(0, len(data), 7):
        group = data[i:i + 7]
        head = 0
        for j, b in enumerate(group):
            if b & 0x80:
                head |= 0x40 >> j
        out.append(head)
        out.extend(b & 0x7F for b in group)
    return out


def crc32(data):
    # Updater uses the standard table with init 0xFFFFFFFF but no final XOR.
    return zlib.crc32(data) ^ 0xFFFFFFFF


def msg_can_update(dev):
    return [0x52, 0x00, dev, 0x01]


def msg_packet_processed(dev):
    return [0x52, 0x00, dev, 0x04]


def msg_erase(dev, addr, size):
    return [0x52, 0x00, dev, 0x0A] + septets(addr, 5) + septets(size, 5)


def msg_write(dev, addr, chunk):
    return ([0x52, 0x00, dev, 0x02] + septets(addr, 5) + septets(len(chunk), 2)
            + convert8to7(chunk) + septets(crc32(chunk), 5))


class Pedal:
    def __init__(self, port_name, log=None):
        self.out = mido.open_output(port_name)
        self.inp = mido.open_input(port_name)
        self.log = log
        self.dev = None

    def _log(self, direction, data):
        if self.log:
            self.log.write(f"{time.time():.3f} {direction} {' '.join(f'{b:02x}' for b in data)}\n")

    def drain(self):
        for _ in self.inp.iter_pending():
            pass

    def send(self, data):
        self._log('>>', data)
        self.out.send(mido.Message('sysex', data=data))

    def recv(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m in self.inp.iter_pending():
                if m.type == 'sysex':
                    self._log('<<', m.data)
                    return list(m.data)
            time.sleep(0.001)
        return None

    def identify(self):
        self.drain()
        self.send([0x7E, 0x00, 0x06, 0x01])
        r = self.recv(3)
        if not r or r[:4] != [0x7E, 0x00, 0x06, 0x02] or r[4] != 0x52:
            raise RuntimeError(f"bad identity reply: {r}")
        self.dev = r[5]
        version = bytes(r[9:13]).decode('ascii', 'replace')
        return self.dev, version

    def command(self, data, timeout, what):
        self.send(data)
        r = self.recv(timeout)
        if r is None:
            raise RuntimeError(f"{what}: no reply in {timeout}s")
        if r[:4] != [0x52, 0x00, self.dev, 0x00] or len(r) < 5 or r[4] != 0x00:
            raise RuntimeError(f"{what}: error reply {' '.join(f'{b:02x}' for b in r)}")

    def erase(self, addr, size):
        self.command(msg_erase(self.dev, addr, size), 120, f"erase 0x{addr:06X}+0x{size:X}")

    def write(self, addr, data, label):
        total = len(data)
        for off in range(0, total, CHUNK):
            chunk = data[off:off + CHUNK]
            for attempt in range(3):
                try:
                    self.command(msg_write(self.dev, addr + off, chunk), 15,
                                 f"write 0x{addr + off:06X}")
                    break
                except RuntimeError as e:
                    if attempt == 2:
                        raise
                    print(f"\n  retry ({e})")
            done = off + len(chunk)
            print(f"\r  {label}: {done * 100 // total:3d}%  {done}/{total}", end='', flush=True)
        print()


def find_port(hint):
    names = mido.get_output_names()
    for n in names:
        if hint.lower() in n.lower():
            return n
    raise SystemExit(f"MIDI port matching '{hint}' not found. Available: {names}")


def load_images(args):
    images = []
    if args.full:
        for name in FULL_ORDER:
            path = os.path.join(args.full, name)
            if name == 'FS.bin' and args.fs:
                path = args.fs
            images.append((name, path))
    else:
        images.append(('FS.bin', args.fs))

    loaded = []
    for name, path in images:
        data = open(path, 'rb').read()
        addr, area = LAYOUT[name]
        if len(data) > area:
            raise SystemExit(f"{path}: {len(data)} bytes exceeds flash area 0x{area:X}")
        if name == 'FS.bin' and data[:6] != b'\x55\xAA\x00\x01\x04\x00':
            raise SystemExit(f"{path}: not an FS.bin image (bad magic)")
        loaded.append((name, path, addr, area, data))
    return loaded


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('fs', nargs='?', help='FS.bin to flash')
    ap.add_argument('--full', metavar='DIR',
                    help='flash boot/Preset/FS/Main like the official updater, from DIR '
                         '(e.g. "...Updater.app/Contents/Resources"); FS.bin overridable by positional arg')
    ap.add_argument('--port', default='ZOOM MS', help='MIDI port name substring (default: "ZOOM MS")')
    ap.add_argument('--dry-run', action='store_true', help='build all packets, do not touch the pedal')
    ap.add_argument('--log', help='write raw SysEx traffic to this file')
    ap.add_argument('-y', '--yes', action='store_true', help='do not ask for confirmation')
    args = ap.parse_args()
    if not args.fs and not args.full:
        ap.error('give an FS.bin path and/or --full DIR')

    images = load_images(args)
    print("Plan:")
    print(f"  erase 0x{MARKER_ERASE[0]:06X} +0x{MARKER_ERASE[1]:X}")
    for name, path, addr, area, data in images:
        print(f"  erase 0x{addr:06X} +0x{area:X}, write {name} ({len(data)} bytes, "
              f"{-(-len(data) // CHUNK)} packets) <- {path}")

    if args.dry_run:
        n = 0
        for name, path, addr, area, data in images:
            for off in range(0, len(data), CHUNK):
                pkt = msg_write(0x61, addr + off, data[off:off + CHUNK])
                assert all(b < 0x80 for b in pkt)
                n += 1
        print(f"Dry run OK: {n} write packets built, all 7-bit clean.")
        return

    log = open(args.log, 'w') if args.log else None
    pedal = Pedal(find_port(args.port), log)
    dev, version = pedal.identify()
    print(f"\nDevice: {DEVICES.get(dev, f'0x{dev:02X}')} (id 0x{dev:02X}), boot/firmware version {version}")
    if dev not in DEVICES:
        raise SystemExit("Unknown device id, aborting.")

    if not args.yes:
        if input("Flash now? Do NOT unplug during flashing. [y/N] ").strip().lower() != 'y':
            raise SystemExit("Aborted.")

    t0 = time.time()
    pedal.command(msg_can_update(dev), 5, 'CanUpdate')
    print("Update mode accepted.")
    pedal.erase(*MARKER_ERASE)
    for name, path, addr, area, data in images:
        print(f"Erasing {name} area 0x{addr:06X}...")
        pedal.erase(addr, area)
        pedal.write(addr, data, name)
    pedal.command(msg_packet_processed(dev), 30, 'PacketProcessed')
    print(f"\nDone in {time.time() - t0:.0f}s. Power-cycle the pedal.")
    if log:
        log.close()


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as e:
        print(f"\nERROR: {e}\nPedal stays in update mode; re-run this script or the official updater.")
        sys.exit(1)
