"""Enumerate a server's shares over SRVSVC, without shelling out to Samba.

Listing shares is not part of SMB. It is a separate RPC service, MS-SRVS, spoken
over DCE/RPC on the ``\\srvsvc`` named pipe inside the ``IPC$`` share — which is
why every SMB client implements it by hand or shells out to one that has.

NASync used to run ``smbclient -L``. That works on a developer's machine and
**not inside a Flatpak**, where the Samba client binaries aren't present and
bundling the whole of Samba to list share names would be an absurd thing to ask
a reviewer to accept. So the exchange is spoken directly here: one bind, one
call, no subprocess and no new dependency.

The exchange is deliberately the minimum that answers the question:

    bind    → SRVSVC v3.0 over NDR v2.0
    call    → NetrShareEnum (opnum 15) at level 1
    decode  → name, type and comment for each share

Level 1 is chosen because it is what ``smbclient -L`` shows and all NASync
needs. Higher levels return permissions and connection counts that we would
only throw away, and some servers refuse them for a non-administrator.

Everything here is read-only. There is no code in this module that can change
anything on the server.
"""

import struct
import uuid

#: MS-SRVS interface, version 3.0 — the service that knows about shares.
SRVSVC_SYNTAX = (uuid.UUID("4b324fc8-1670-01d3-1278-5a47bf6ee188"), 3, 0)
#: The NDR transfer syntax, version 2.0 — how the arguments are marshalled.
NDR_SYNTAX = (uuid.UUID("8a885d04-1ceb-11c9-9fe8-08002b104860"), 2, 0)

#: DCE/RPC packet types we send and expect back.
PTYPE_REQUEST, PTYPE_RESPONSE, PTYPE_BIND, PTYPE_BIND_ACK = 0, 2, 11, 12
#: PFC_FIRST_FRAG | PFC_LAST_FRAG — every PDU here is a complete message.
PFC_FIRST_LAST = 0x03
#: Little-endian, ASCII, IEEE float. The only representation anything sane uses.
DATA_REPRESENTATION = b"\x10\x00\x00\x00"

#: NetrShareEnum. See MS-SRVS 3.1.4.8.
OPNUM_NETR_SHARE_ENUM = 15
SHARE_INFO_LEVEL = 1

#: Share types (MS-SRVS 2.2.2.4). The low bits are the kind; the high bits are
#: flags, of which STYPE_SPECIAL marks the administrative shares (C$, ADMIN$).
STYPE_MASK = 0x0FFFFFFF
STYPE_DISKTREE, STYPE_PRINTQ, STYPE_DEVICE, STYPE_IPC = 0, 1, 2, 3
STYPE_SPECIAL = 0x80000000
STYPE_TEMPORARY = 0x40000000

KIND_NAMES = {STYPE_DISKTREE: "Disk", STYPE_PRINTQ: "Printer",
              STYPE_DEVICE: "Device", STYPE_IPC: "IPC"}

#: NetrShareEnum's return value. 0 is success; ERROR_MORE_DATA means the reply
#: was truncated but what arrived is valid. Anything else — most usefully
#: ERROR_ACCESS_DENIED (5) from a NAS that restricts SRVSVC to administrators —
#: is a failure that must not be mistaken for "this server has no shares".
ERROR_SUCCESS, ERROR_MORE_DATA = 0, 234

#: One read has to hold the whole reply. A home NAS has a handful of shares,
#: so this is roomy enough that PDU fragmentation never comes up.
MAX_PDU_BYTES = 1024 * 1024

#: What a server may return in one go. Servers cap this themselves; asking for
#: everything is what `smbclient -L` does too.
MAX_RESPONSE_BYTES = 0xFFFFFFFF


class SrvsvcError(Exception):
    """The server couldn't be asked, or didn't answer in a shape we understand."""


def _syntax(spec) -> bytes:
    identifier, major, minor = spec
    return identifier.bytes_le + struct.pack("<HH", major, minor)


def _bind_pdu(call_id: int = 1) -> bytes:
    """Offer one presentation context: SRVSVC carried over NDR."""
    body = struct.pack("<HHI", 4280, 4280, 0)      # max xmit, max recv, assoc group
    body += struct.pack("<B3x", 1)                 # one context element, padded
    body += struct.pack("<HBB", 0, 1, 0)           # context id 0, one transfer syntax
    body += _syntax(SRVSVC_SYNTAX)
    body += _syntax(NDR_SYNTAX)
    return _header(PTYPE_BIND, len(body), call_id) + body


def _request_pdu(opnum: int, stub: bytes, call_id: int = 2) -> bytes:
    body = struct.pack("<IHH", len(stub), 0, opnum)   # alloc hint, context id, opnum
    return _header(PTYPE_REQUEST, len(body) + len(stub), call_id) + body + stub


def _header(ptype: int, body_length: int, call_id: int) -> bytes:
    return struct.pack("<BBBB4sHHI", 5, 0, ptype, PFC_FIRST_LAST,
                       DATA_REPRESENTATION, 16 + body_length, 0, call_id)


def _share_enum_stub() -> bytes:
    """Arguments for NetrShareEnum: "level 1, everything, from the start".

    ``ServerName`` and ``ResumeHandle`` are both NULL unique pointers — the
    server knows its own name, and one call returns the whole list for the
    share counts a home NAS has.
    """
    return struct.pack(
        "<IIIIIII",
        0,                     # ServerName: NULL
        SHARE_INFO_LEVEL,      # InfoStruct.Level
        SHARE_INFO_LEVEL,      # union discriminant, same value
        0,                     # EntriesRead in — nothing to send
        0,                     # Buffer in — NULL
        MAX_RESPONSE_BYTES,    # PreferedMaximumLength
        0,                     # ResumeHandle: NULL
    )


class _Reader:
    """A cursor over NDR-encoded bytes.

    NDR aligns each primitive to its own size, and strings are padded out to a
    4-byte boundary. Getting that wrong shifts everything after it, so the
    alignment lives here rather than being remembered at each call site.
    """

    def __init__(self, data: bytes):
        self._data = data
        self._at = 0

    def uint32(self) -> int:
        if self._at + 4 > len(self._data):
            raise SrvsvcError("The server's reply ended in the middle of a field.")
        value, = struct.unpack_from("<I", self._data, self._at)
        self._at += 4
        return value

    def string(self) -> str:
        """A conformant *varying* string: max count, offset, then the characters."""
        max_count = self.uint32()
        offset = self.uint32()
        actual = self.uint32()
        del max_count
        start = self._at + offset * 2
        end = start + actual * 2
        if end > len(self._data):
            raise SrvsvcError("The server's reply ended inside a share name.")
        text = self._data[start:end].decode("utf-16-le", errors="replace")
        self._at = end
        # Every string is followed by padding to the next 4-byte boundary.
        self._at += (-self._at) % 4
        return text.rstrip("\x00")


def parse_share_enum(stub: bytes) -> list:
    """Decode a NetrShareEnum level-1 reply into share dictionaries.

    Separate from the I/O above so it can be tested against captured bytes —
    which is the only practical way to cover a wire format, since a fake that
    produces what the parser expects proves nothing about a real server.
    """
    # The return code is the last field of the stub, after a resume handle that
    # may or may not be present — so it is read from the end rather than by
    # parsing forward through an optional field. Checked *first*: a refusal
    # arrives as a well-formed reply with an empty container, and returning []
    # for it told three separate surfaces that a server full of shares had none.
    if len(stub) >= 4:
        status, = struct.unpack_from("<I", stub, len(stub) - 4)
        if status not in (ERROR_SUCCESS, ERROR_MORE_DATA):
            raise SrvsvcError(
                "The server refused to list its shares"
                + (" (access denied — it may only allow administrators to)."
                   if status == 5 else f" (error {status})."))

    reader = _Reader(stub)
    level = reader.uint32()
    if level != SHARE_INFO_LEVEL:
        raise SrvsvcError(f"The server answered at level {level}, not 1.")
    reader.uint32()                      # union discriminant, repeats the level
    if reader.uint32() == 0:             # pointer to the container
        return []                        # a server with nothing to show
    entries = reader.uint32()
    if reader.uint32() == 0:             # pointer to the array of entries
        return []
    reader.uint32()                      # conformant array max count

    # Two passes, because that is how NDR lays it out: the fixed part of every
    # element first, then the variable-length parts in the same order.
    fixed = []
    for _ in range(entries):
        name_ptr = reader.uint32()
        share_type = reader.uint32()
        comment_ptr = reader.uint32()
        fixed.append((name_ptr, share_type, comment_ptr))

    shares = []
    for name_ptr, share_type, comment_ptr in fixed:
        name = reader.string() if name_ptr else ""
        comment = reader.string() if comment_ptr else ""
        kind = share_type & STYPE_MASK
        shares.append({
            "name": name,
            "comment": comment,
            "type": KIND_NAMES.get(kind, f"Unknown ({kind})"),
            "is_disk": kind == STYPE_DISKTREE,
            # Administrative shares — C$, ADMIN$, IPC$. Present on every
            # Windows box and never what someone means to sync to.
            "is_special": bool(share_type & STYPE_SPECIAL),
            "is_temporary": bool(share_type & STYPE_TEMPORARY),
        })
    return shares


def enumerate_shares(pipe) -> list:
    """Bind to SRVSVC on an open ``\\srvsvc`` pipe and list the shares.

    `pipe` is a file object on ``\\\\server\\IPC$\\srvsvc`` opened for reading
    and writing, unbuffered.

    The seeks are not decoration. A named pipe is a message channel, but the
    file object wrapping it counts bytes like an ordinary file — so after
    writing a request its offset sits past the reply, and reading without
    rewinding returns nothing at all. That failure looks exactly like a server
    that refused the call.
    """
    try:
        pipe.seek(0)
        pipe.write(_bind_pdu())
        pipe.seek(0)
        ack = pipe.read(4280)
    except Exception as e:
        raise SrvsvcError(f"Couldn't reach the server's share service: {e}")

    if len(ack) < 4 or ack[2] != PTYPE_BIND_ACK:
        raise SrvsvcError("The server refused to talk about its shares.")

    try:
        pipe.seek(0)
        pipe.write(_request_pdu(OPNUM_NETR_SHARE_ENUM, _share_enum_stub()))
        pipe.seek(0)
        reply = pipe.read(MAX_PDU_BYTES)
    except Exception as e:
        raise SrvsvcError(f"Couldn't read the share list: {e}")

    if len(reply) < 24 or reply[2] != PTYPE_RESPONSE:
        raise SrvsvcError("The server didn't answer the share query.")
    return parse_share_enum(reply[24:])


