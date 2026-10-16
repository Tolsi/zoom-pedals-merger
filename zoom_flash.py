#!/usr/bin/env python3
"""
Zoom MultiStomp firmware flasher — writes FS.bin to pedal via MIDI SysEx.

Uses macOS CoreMIDI directly via ctypes (no compiled dependencies).

The pedal must be in update mode: hold both arrow buttons while connecting USB.
It will show up as "ZOOM MS Series" MIDI device.

Protocol reverse-engineered from the official EFX Updater binary.

Usage:
    python3 zoom_flash.py /path/to/FS.bin

WARNING: This can brick your pedal. Use at your own risk.
Keep the original updater app and stock FS.bin as backup.
"""

import ctypes
import ctypes.util
import struct
import time
import sys
import os
import threading

# CoreMIDI constants
kCFStringEncodingUTF8 = 0x08000100

# Zoom SysEx protocol
ZOOM_MANUFACTURER_ID = 0x52
MS70CDR_DEVICE_ID = 0x61
MS50G_DEVICE_ID = 0x58
MS60B_DEVICE_ID = 0x5F

# Flash addresses from ROM procedure in EFX Updater binary
FLASH_MAP = {
    'FS.bin': {'address': 0x040000, 'erase_size': 0x0B0000},
    'Preset.bin': {'address': 0x000000, 'erase_size': 0x040000},
    'boot.bin': {'address': 0x3FD000, 'erase_size': 0x003000},
    'Main.bin': {'address': 0x0F0000, 'erase_size': 0x2C7000},
}

FLASH_WRITE_CHUNK = 64


class CoreMIDI:
    """Minimal CoreMIDI wrapper via ctypes for macOS."""

    def __init__(self):
        self.cm = ctypes.cdll.LoadLibrary(
            '/System/Library/Frameworks/CoreMIDI.framework/CoreMIDI')
        self.cf = ctypes.cdll.LoadLibrary(
            '/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')

        self._setup_types()

        self.client = ctypes.c_uint32()
        name = self._cfstr("ZoomFlasher")
        err = self.cm.MIDIClientCreate(name, None, None, ctypes.byref(self.client))
        if err != 0:
            raise RuntimeError(f"MIDIClientCreate failed: {err}")

        self.out_port = ctypes.c_uint32()
        err = self.cm.MIDIOutputPortCreate(
            self.client, self._cfstr("Out"), ctypes.byref(self.out_port))
        if err != 0:
            raise RuntimeError(f"MIDIOutputPortCreate failed: {err}")

        self.in_port = ctypes.c_uint32()
        self._received = []
        self._lock = threading.Lock()

        self._READPROC = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
        self._read_callback = self._READPROC(self._midi_read)

        err = self.cm.MIDIInputPortCreate(
            self.client, self._cfstr("In"),
            self._read_callback, None,
            ctypes.byref(self.in_port))
        if err != 0:
            raise RuntimeError(f"MIDIInputPortCreate failed: {err}")

    def _setup_types(self):
        self.cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self.cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        self.cf.CFStringGetCString.restype = ctypes.c_bool
        self.cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        self.cf.CFStringGetLength.restype = ctypes.c_long
        self.cf.CFStringGetLength.argtypes = [ctypes.c_void_p]

        self.cm.MIDIObjectGetStringProperty.restype = ctypes.c_int32
        self.cm.MIDIObjectGetStringProperty.argtypes = [
            ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.cm.MIDISendSysex.restype = ctypes.c_int32

    def _cfstr(self, s):
        return self.cf.CFStringCreateWithCString(None, s.encode(), kCFStringEncodingUTF8)

    def _get_name(self, endpoint):
        prop = self._cfstr("name")
        result = ctypes.c_void_p()
        err = self.cm.MIDIObjectGetStringProperty(endpoint, prop, ctypes.byref(result))
        if err == 0 and result.value:
            buf = ctypes.create_string_buffer(256)
            self.cf.CFStringGetCString(result.value, buf, 256, kCFStringEncodingUTF8)
            return buf.value.decode('utf-8')
        return ""

    def _midi_read(self, pkt_list_ptr, read_proc_ref_con):
        # MIDIPacketList: numPackets (UInt32), then packets
        # MIDIPacket: timeStamp (UInt64), length (UInt16), data[length]
        if not pkt_list_ptr:
            return
        num_packets = ctypes.cast(pkt_list_ptr, ctypes.POINTER(ctypes.c_uint32))[0]
        offset = 4  # skip numPackets
        base = ctypes.cast(pkt_list_ptr, ctypes.POINTER(ctypes.c_uint8))
        for _ in range(num_packets):
            # timestamp: 8 bytes, length: 2 bytes
            length = (base[offset + 8] | (base[offset + 9] << 8))
            data = bytes(base[offset + 10 + j] for j in range(length))
            with self._lock:
                self._received.append(data)
            offset += 10 + length
            # Align to 4 bytes (MIDIPacket alignment)
            # Actually MIDIPacket uses MIDIPacketNext which handles alignment
            # For simplicity, break after first packet in most cases
            break

    def find_zoom(self):
        n_dest = self.cm.MIDIGetNumberOfDestinations()
        n_src = self.cm.MIDIGetNumberOfSources()

        dest_ep = None
        src_ep = None

        for i in range(n_dest):
            ep = self.cm.MIDIGetDestination(i)
            name = self._get_name(ep)
            if 'zoom' in name.lower() or 'ms' in name.lower():
                dest_ep = ep
                break

        for i in range(n_src):
            ep = self.cm.MIDIGetSource(i)
            name = self._get_name(ep)
            if 'zoom' in name.lower() or 'ms' in name.lower():
                src_ep = ep
                break

        return dest_ep, src_ep

    def connect_source(self, src_ep):
        err = self.cm.MIDIPortConnectSource(self.in_port, src_ep, None)
        if err != 0:
            raise RuntimeError(f"MIDIPortConnectSource failed: {err}")

    def send_sysex(self, data):
        """Send a SysEx message (must include F0 and F7)."""
        # MIDISysexSendRequest struct:
        # MIDIEndpointRef destination (4 bytes on 32-bit, but we use 64-bit layout)
        # Actually on macOS 64-bit, MIDISysexSendRequest is:
        #   destination: MIDIEndpointRef (UInt32)
        #   data: const Byte * (8 bytes pointer)
        #   bytesToSend: UInt32
        #   complete: Boolean (1 byte)
        #   3 bytes padding
        #   completionProc: pointer (8)
        #   completionRefCon: pointer (8)
        # Total: 4 + 4pad + 8 + 4 + 4 + 8 + 8 = 40 bytes
        #
        # Use a simpler approach: MIDISend with a packet list
        pass

    def send_raw(self, dest_ep, data):
        """Send raw MIDI bytes via MIDISend."""
        # Build a MIDIPacketList
        # numPackets: UInt32 = 1
        # MIDIPacket: timeStamp(UInt64=0) + length(UInt16) + data
        pkt_size = 4 + 8 + 2 + len(data)
        buf = ctypes.create_string_buffer(pkt_size)
        struct.pack_into('<I', buf, 0, 1)  # numPackets = 1
        struct.pack_into('<Q', buf, 4, 0)  # timeStamp = 0 (now)
        struct.pack_into('<H', buf, 12, len(data))  # length
        for i, b in enumerate(data):
            buf[14 + i] = b

        err = self.cm.MIDISend(self.out_port, dest_ep, buf)
        if err != 0:
            raise RuntimeError(f"MIDISend failed: {err}")

    def send_sysex_async(self, dest_ep, data):
        """Send SysEx via MIDISendSysex (handles large messages)."""
        data_buf = (ctypes.c_uint8 * len(data))(*data)

        class MIDISysexSendRequest(ctypes.Structure):
            _fields_ = [
                ("destination", ctypes.c_uint32),
                ("_pad0", ctypes.c_uint32),
                ("data", ctypes.POINTER(ctypes.c_uint8)),
                ("bytesToSend", ctypes.c_uint32),
                ("complete", ctypes.c_uint8),
                ("_pad1", ctypes.c_uint8 * 3),
                ("completionProc", ctypes.c_void_p),
                ("completionRefCon", ctypes.c_void_p),
            ]

        req = MIDISysexSendRequest()
        req.destination = dest_ep
        req.data = data_buf
        req.bytesToSend = len(data)
        req.complete = 0
        req.completionProc = None
        req.completionRefCon = None

        err = self.cm.MIDISendSysex(ctypes.byref(req))
        if err != 0:
            raise RuntimeError(f"MIDISendSysex failed: {err}")

        # Wait for completion
        for _ in range(200):
            if req.complete:
                break
            time.sleep(0.01)

    def receive(self, timeout=2.0):
        """Wait for a MIDI message."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._received:
                    return self._received.pop(0)
            time.sleep(0.01)
        return None

    def clear_received(self):
        with self._lock:
            self._received.clear()


def encode_7bit(data):
    """Encode 8-bit data to 7-bit MIDI format.

    For every 7 bytes of input, output 8 bytes:
    first byte = MSBs of next 7 bytes, then 7 bytes with MSB cleared.
    """
    result = bytearray()
    for i in range(0, len(data), 7):
        chunk = data[i:i+7]
        msb_byte = 0
        for j, b in enumerate(chunk):
            if b & 0x80:
                msb_byte |= (1 << j)
        result.append(msb_byte)
        for b in chunk:
            result.append(b & 0x7F)
    return bytes(result)


def make_sysex(device_id, *payload):
    """Build a Zoom SysEx message: F0 52 00 <dev_id> <payload> F7."""
    msg = bytearray([0xF0, ZOOM_MANUFACTURER_ID, 0x00, device_id])
    for b in payload:
        if isinstance(b, (bytes, bytearray)):
            msg.extend(b)
        else:
            msg.append(b)
    msg.append(0xF7)
    return bytes(msg)


def encode_address(addr):
    """Encode a 32-bit address as 4 x 7-bit bytes (LSB first)."""
    return bytes([
        addr & 0x7F,
        (addr >> 7) & 0x7F,
        (addr >> 14) & 0x7F,
        (addr >> 21) & 0x7F,
    ])


def encode_size(size):
    """Encode a 32-bit size as 4 x 7-bit bytes (LSB first)."""
    return encode_address(size)


def identity_request():
    """Universal SysEx Identity Request."""
    return bytes([0xF0, 0x7E, 0x00, 0x06, 0x01, 0xF7])


def parse_identity(data):
    """Parse Identity Reply. Returns (manufacturer, family, model, version) or None."""
    if len(data) < 15 or data[0] != 0xF0 or data[1] != 0x7E:
        return None
    if data[3] != 0x06 or data[4] != 0x02:
        return None
    mfr = data[5]
    family = data[6] | (data[7] << 7)
    model = data[8] | (data[9] << 7)
    version = ''.join(chr(b) for b in data[10:14] if 0x20 <= b <= 0x7E)
    return mfr, family, model, version


def flash_firmware(fsbin_path, device_id=None):
    """Flash FS.bin to a Zoom MultiStomp pedal in update mode."""

    if not os.path.exists(fsbin_path):
        print(f"Error: {fsbin_path} not found")
        sys.exit(1)

    with open(fsbin_path, 'rb') as f:
        firmware = f.read()

    # Verify it looks like a valid FS.bin
    if len(firmware) < 0xA000:
        print(f"Error: file too small ({len(firmware)} bytes)")
        sys.exit(1)
    if firmware[0:6] != b'\x55\xAA\x00\x01\x04\x00':
        print("Warning: file doesn't start with FS.bin magic bytes (55 AA 00 01 04 00)")
        resp = input("Continue anyway? [y/N] ")
        if resp.lower() != 'y':
            sys.exit(1)

    flash_addr = FLASH_MAP['FS.bin']['address']
    erase_size = FLASH_MAP['FS.bin']['erase_size']

    print(f"Firmware: {fsbin_path}")
    print(f"Size: {len(firmware)} bytes ({len(firmware)//1024} KB)")
    print(f"Flash address: 0x{flash_addr:06X}")
    print(f"Erase size: 0x{erase_size:06X} ({erase_size//1024} KB)")
    print()

    midi = CoreMIDI()
    dest_ep, src_ep = midi.find_zoom()

    if not dest_ep or not src_ep:
        print("Error: Zoom MS Series MIDI device not found!")
        print("Make sure the pedal is connected in update mode")
        print("(hold both arrow buttons while plugging USB)")
        sys.exit(1)

    print("Found ZOOM MS Series")
    midi.connect_source(src_ep)
    time.sleep(0.1)

    # Step 1: Identity Request
    print("Sending Identity Request...")
    midi.clear_received()
    midi.send_sysex_async(dest_ep, identity_request())
    reply = midi.receive(timeout=3.0)

    if reply is None:
        print("Error: No reply from device")
        sys.exit(1)

    ident = parse_identity(reply)
    if ident is None:
        print(f"Error: Invalid identity reply: {reply.hex(' ')}")
        sys.exit(1)

    mfr, family, model, version = ident
    if mfr != ZOOM_MANUFACTURER_ID:
        print(f"Error: Not a Zoom device (manufacturer 0x{mfr:02X})")
        sys.exit(1)

    dev_id = family  # Zoom puts device ID in family field
    device_names = {
        MS70CDR_DEVICE_ID: "MS-70CDR",
        MS50G_DEVICE_ID: "MS-50G",
        MS60B_DEVICE_ID: "MS-60B",
    }
    dev_name = device_names.get(dev_id, f"Unknown (0x{dev_id:02X})")
    print(f"Device: {dev_name} (ID: 0x{dev_id:02X}), firmware: {version}")

    if device_id and dev_id != device_id:
        print(f"Error: Expected device 0x{device_id:02X}, got 0x{dev_id:02X}")
        sys.exit(1)

    # Step 2: Send "Can Update" / enter update mode
    print("\nSending update request...")
    midi.clear_received()
    can_update_msg = make_sysex(dev_id, 0x60)
    midi.send_sysex_async(dest_ep, can_update_msg)
    reply = midi.receive(timeout=3.0)
    if reply:
        print(f"  Reply: {reply.hex(' ')}")

    # Step 3: Flash Erase
    print(f"\nErasing flash at 0x{flash_addr:06X}, size 0x{erase_size:06X}...")
    erase_msg = make_sysex(dev_id, 0x71,
                           encode_address(flash_addr),
                           encode_size(erase_size))
    midi.clear_received()
    midi.send_sysex_async(dest_ep, erase_msg)
    # Erase takes time — wait for ACK
    reply = midi.receive(timeout=30.0)
    if reply:
        print(f"  Erase reply: {reply.hex(' ')}")
    else:
        print("  Warning: No erase reply (continuing...)")

    # Step 4: Flash Write in chunks
    print(f"\nWriting {len(firmware)} bytes...")
    chunk_size = 256  # bytes per SysEx write
    total_chunks = (len(firmware) + chunk_size - 1) // chunk_size

    for i in range(total_chunks):
        offset = i * chunk_size
        chunk = firmware[offset:offset + chunk_size]
        addr = flash_addr + offset

        encoded_data = encode_7bit(chunk)
        write_msg = make_sysex(dev_id, 0x72,
                               encode_address(addr),
                               encode_size(len(chunk)),
                               encoded_data)

        midi.send_sysex_async(dest_ep, write_msg)

        # Wait for ACK between chunks
        reply = midi.receive(timeout=5.0)
        if reply and len(reply) > 4:
            # Check for error in reply
            if reply[4] == 0x00:  # Error response
                print(f"\n  Error at offset 0x{offset:06X}: {reply.hex(' ')}")
                sys.exit(1)

        if (i + 1) % 50 == 0 or i == total_chunks - 1:
            pct = (i + 1) * 100 // total_chunks
            print(f"  [{pct:3d}%] {offset + len(chunk):>7d}/{len(firmware)} bytes", end='\r')

    print()

    # Step 5: Done Update
    print("\nSending update complete...")
    done_msg = make_sysex(dev_id, 0x61)
    midi.send_sysex_async(dest_ep, done_msg)
    reply = midi.receive(timeout=5.0)
    if reply:
        print(f"  Reply: {reply.hex(' ')}")

    print("\nDone! Power-cycle the pedal to boot with new firmware.")
    print("If the pedal doesn't boot, re-flash with the original updater app.")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 zoom_flash.py <FS.bin>")
        print()
        print("Flash a modified FS.bin to a Zoom MultiStomp pedal.")
        print("The pedal must be in update mode (hold both arrow buttons + USB).")
        print()
        print("Supported pedals: MS-50G, MS-60B, MS-70CDR")
        sys.exit(1)

    fsbin_path = sys.argv[1]
    flash_firmware(fsbin_path)


if __name__ == '__main__':
    main()
