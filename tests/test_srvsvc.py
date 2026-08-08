"""SRVSVC share enumeration — the DCE/RPC exchange that replaced `smbclient -L`.

The decoder is tested against **bytes captured from a real Samba server**, not
against a fake that produces what the parser expects. For a wire format that is
the only test worth having: a hand-written fixture that agrees with the parser
proves the two agree with each other, and nothing about the protocol.

The capture below is one `NetrShareEnum` level-1 reply from `dperson/samba`
serving two disk shares plus `IPC$`.
"""

import struct

import pytest

from nasync.core import srvsvc

#: Stub data (the bytes after the 24-byte DCE/RPC response header) from a real
#: server offering `backup`, `media` and `IPC$`.
REAL_REPLY = bytes.fromhex(
    "0100000001000000040002000300000008000200030000000c000200000000001000"
    "0200140002000000000018000200 1c00020003000080200002000700000000000000"
    "070000006200610063006b007500700000000000010000000000000001000000000000"
    "000600000000000000060000006d0065006400690061000000010000000000000001"
    "0000000000000005000000000000000500000049005000430024000000000 01b0000"
    "00000000001b0000004900500043002000530065007200760069006300650020002800"
    "530061006d0062006100200053006500720076006500720029000000000003000000"
    "240002000000000000000000".replace(" ", "")
)


def test_a_real_reply_decodes_to_the_shares_the_server_offers():
    shares = srvsvc.parse_share_enum(REAL_REPLY)
    assert [s["name"] for s in shares] == ["backup", "media", "IPC$"]
    assert [s["type"] for s in shares] == ["Disk", "Disk", "IPC"]


def test_administrative_shares_are_flagged_not_silently_dropped():
    """The decoder reports; choosing what to hide is the caller's job — the
    server-info panel wants to show IPC$, the share picker doesn't."""
    shares = {s["name"]: s for s in srvsvc.parse_share_enum(REAL_REPLY)}
    assert shares["IPC$"]["is_special"] is True
    assert shares["IPC$"]["is_disk"] is False
    assert shares["backup"]["is_special"] is False
    assert shares["backup"]["is_disk"] is True


def test_comments_survive_the_utf16_decoding():
    shares = {s["name"]: s for s in srvsvc.parse_share_enum(REAL_REPLY)}
    assert shares["IPC$"]["comment"] == "IPC Service (Samba Server)"
    assert shares["backup"]["comment"] == ""


def test_a_server_with_no_shares_is_empty_not_an_error():
    """Level, discriminant, NULL container — and a success return code."""
    assert srvsvc.parse_share_enum(struct.pack("<IIII", 1, 1, 0, 0)) == []


def test_a_refusal_is_not_reported_as_an_empty_share_list():
    """A NAS that restricts SRVSVC to administrators answers with a valid,
    empty reply and ERROR_ACCESS_DENIED. Reading only the container told the
    setup flow, the server dialog and the info panel that a server full of
    shares had none."""
    refusal = struct.pack("<IIII", 1, 1, 0, 5)
    with pytest.raises(srvsvc.SrvsvcError, match="access denied"):
        srvsvc.parse_share_enum(refusal)


def test_a_truncated_but_valid_reply_is_still_decoded():
    """ERROR_MORE_DATA means "there was more", not "this is wrong"."""
    assert srvsvc.parse_share_enum(struct.pack("<IIII", 1, 1, 0, 234)) == []


def test_a_truncated_reply_is_refused_rather_than_half_decoded():
    """A short read must not yield a plausible-looking partial share list."""
    with pytest.raises(srvsvc.SrvsvcError):
        srvsvc.parse_share_enum(REAL_REPLY[:40])


def test_an_unexpected_level_is_refused():
    with pytest.raises(srvsvc.SrvsvcError, match="level 2"):
        srvsvc.parse_share_enum(struct.pack("<III", 2, 2, 0))


# ---------------------------------------------------------------------- #
# The PDUs we send
# ---------------------------------------------------------------------- #
def test_the_bind_offers_srvsvc_over_ndr():
    pdu = srvsvc._bind_pdu()
    assert pdu[0] == 5 and pdu[1] == 0            # DCE/RPC 5.0
    assert pdu[2] == srvsvc.PTYPE_BIND
    assert srvsvc.SRVSVC_SYNTAX[0].bytes_le in pdu
    assert srvsvc.NDR_SYNTAX[0].bytes_le in pdu


def test_the_request_names_the_share_enum_opnum():
    stub = srvsvc._share_enum_stub()
    pdu = srvsvc._request_pdu(srvsvc.OPNUM_NETR_SHARE_ENUM, stub)
    assert pdu[2] == srvsvc.PTYPE_REQUEST
    alloc_hint, context_id, opnum = struct.unpack_from("<IHH", pdu, 16)
    assert opnum == srvsvc.OPNUM_NETR_SHARE_ENUM
    assert alloc_hint == len(stub)
    assert context_id == 0


def test_the_declared_length_matches_the_bytes_sent():
    """A wrong frag_length is the classic way to make a server hang up."""
    for pdu in (srvsvc._bind_pdu(),
                srvsvc._request_pdu(srvsvc.OPNUM_NETR_SHARE_ENUM,
                                    srvsvc._share_enum_stub())):
        frag_length, = struct.unpack_from("<H", pdu, 8)
        assert frag_length == len(pdu)


# ---------------------------------------------------------------------- #
# The pipe conversation
# ---------------------------------------------------------------------- #
class _FakePipe:
    """A named pipe that answers a bind and then a share enumeration."""

    def __init__(self, bind_ptype=srvsvc.PTYPE_BIND_ACK, reply=REAL_REPLY):
        self._bind_ptype = bind_ptype
        self._reply = reply
        self.writes = []
        self.seeks = []
        self._pending = b""

    def seek(self, offset):
        self.seeks.append(offset)

    def write(self, data):
        self.writes.append(data)
        if data[2] == srvsvc.PTYPE_BIND:
            self._pending = struct.pack("<BBBB4sHHI", 5, 0, self._bind_ptype,
                                        3, b"\x10\x00\x00\x00", 16, 0, 1)
        else:
            header = struct.pack("<BBBB4sHHI", 5, 0, srvsvc.PTYPE_RESPONSE, 3,
                                 b"\x10\x00\x00\x00", 24 + len(self._reply), 0, 2)
            header += struct.pack("<IHBB", len(self._reply), 0, 0, 0)
            self._pending = header + self._reply

    def read(self, _size):
        data, self._pending = self._pending, b""
        return data


def test_the_exchange_binds_before_asking():
    pipe = _FakePipe()
    assert [s["name"] for s in srvsvc.enumerate_shares(pipe)] == \
        ["backup", "media", "IPC$"]
    assert pipe.writes[0][2] == srvsvc.PTYPE_BIND
    assert pipe.writes[1][2] == srvsvc.PTYPE_REQUEST


def test_the_pipe_is_rewound_before_every_read():
    """Regression: without this the reply is read past and comes back empty,
    which looks exactly like a server refusing the call. Cost an hour once."""
    pipe = _FakePipe()
    srvsvc.enumerate_shares(pipe)
    # Two round trips, each seeking before the write and before the read.
    assert pipe.seeks == [0, 0, 0, 0]


def test_a_refused_bind_is_reported_as_such():
    pipe = _FakePipe(bind_ptype=srvsvc.PTYPE_BIND + 100)
    with pytest.raises(srvsvc.SrvsvcError, match="refused to talk"):
        srvsvc.enumerate_shares(pipe)
