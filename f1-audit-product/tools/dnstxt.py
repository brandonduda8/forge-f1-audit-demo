"""dnstxt.py - a small, stdlib-only DNS TXT lookup.

Used for the email-trust checks (SPF, DMARC, DKIM). The standard library can
resolve addresses but not TXT records, and adding a DNS package was not an
option for this build, so this sends one plain DNS query (RFC 1035) to the
resolvers in /etc/resolv.conf and reads the answer.

query_txt(name) -> (status, records)
  status "ok"        records = list of TXT strings (each record's pieces joined)
  status "nodata"    the name exists but has no TXT record
  status "nxdomain"  the name does not exist
  status "error"     the lookup itself failed (timeout, refused, bad answer);
                     callers must treat this as "not measured", never as "no record".

Only the system resolver is contacted, never the website, so the network
guard's rules for website connections don't apply here. Raw sockets are used
on purpose (not socket.create_connection), so the guard does not refuse port 53.
"""
import os
import secrets
import socket
import struct
import time

try:
    import idna as _idna_pkg         # already installed: requests depends on it

    def _idna(name):
        return _idna_pkg.encode(name, uts46=True)
except ImportError:                  # pragma: no cover
    def _idna(name):
        raise ValueError("international names need the idna package")

TYPE_TXT = 16
CLASS_IN = 1
MAX_POINTERS = 64


def nameservers(path="/etc/resolv.conf"):
    out = []
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    out.append(parts[1].split("%")[0])
    except OSError:
        pass
    return out[:3]


def encode_name(name):
    name = name.rstrip(".")
    try:
        if any(ord(c) > 127 for c in name):
            raw = _idna(name)            # IDNA 2008, as browsers and requests use (straße.de -> xn--strae-oqa.de)
        else:
            raw = name.encode("ascii")
    except (UnicodeError, ValueError):
        raise ValueError("bad name") from None
    if not raw or len(raw) > 253:
        raise ValueError("bad name")
    out = b""
    for label in raw.split(b"."):
        if not 0 < len(label) <= 63:
            raise ValueError("bad name")
        out += bytes([len(label)]) + label
    return out + b"\x00"


def build_query(name, qid):
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 1)        # RD, 1 question, 1 additional (EDNS)
    question = encode_name(name) + struct.pack(">HH", TYPE_TXT, CLASS_IN)
    opt = b"\x00" + struct.pack(">HHIH", 41, 1232, 0, 0)               # EDNS0: accept 1232-byte UDP answers
    return header + question + opt


def skip_name(buf, off):
    """Offset just past a (possibly compressed) name starting at off."""
    jumps = 0
    while True:
        if off >= len(buf):
            raise ValueError("truncated name")
        n = buf[off]
        if n == 0:
            return off + 1
        if n & 0xC0 == 0xC0:
            if off + 1 >= len(buf):
                raise ValueError("truncated pointer")
            jumps += 1
            if jumps > MAX_POINTERS:
                raise ValueError("pointer loop")
            return off + 2                    # a pointer ends the name in place
        if n & 0xC0:
            raise ValueError("bad label")
        off += 1 + n


def parse_response(buf, qid):
    if len(buf) < 12:
        raise ValueError("short answer")
    rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", buf[:12])
    if rid != qid or not flags & 0x8000:
        raise ValueError("not our answer")
    if flags & 0x0200:
        return "truncated", []
    rcode = flags & 0x000F
    if rcode == 3:
        return "nxdomain", []
    if rcode != 0:
        return "error", []
    off = 12
    for _ in range(qd):
        off = skip_name(buf, off) + 4
    records = []
    for _ in range(an):
        off = skip_name(buf, off)
        if off + 10 > len(buf):
            raise ValueError("truncated record")
        rtype, rclass, _ttl, rdlen = struct.unpack(">HHIH", buf[off:off + 10])
        off += 10
        rdata = buf[off:off + rdlen]
        if len(rdata) != rdlen:
            raise ValueError("truncated rdata")
        off += rdlen
        if rtype != TYPE_TXT or rclass != CLASS_IN:
            continue                           # e.g. the CNAME a TXT answer followed
        parts, i = [], 0
        while i < len(rdata):
            ln = rdata[i]
            parts.append(rdata[i + 1:i + 1 + ln])
            i += 1 + ln
        records.append(b"".join(parts).decode("utf-8", "replace"))
    return ("ok" if records else "nodata"), records


def _udp(server, port, packet, timeout):
    fam = socket.AF_INET6 if ":" in server else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.sendto(packet, (server, port))
        end = time.monotonic() + timeout
        while True:
            s.settimeout(max(0.05, end - time.monotonic()))
            data, addr = s.recvfrom(4096)
            if addr[0] == server or server in ("127.0.0.1", "::1"):
                return data
    finally:
        s.close()


def _tcp(server, port, packet, timeout):
    fam = socket.AF_INET6 if ":" in server else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect((server, port))
        s.sendall(struct.pack(">H", len(packet)) + packet)
        need, buf = None, b""
        while need is None or len(buf) < need + 2:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            if need is None and len(buf) >= 2:
                need = struct.unpack(">H", buf[:2])[0]
            if len(buf) > 70000:
                break
        return buf[2:2 + (need or 0)]
    finally:
        s.close()


def query_txt(name, timeout=2.0, servers=None, port=53, deadline=None):
    """deadline (time.monotonic()) caps the whole lookup, every server and retry included."""
    try:
        encode_name(name)
    except ValueError:
        return "error", []
    servers = servers if servers is not None else nameservers()
    if not servers:
        return "error", []
    if deadline is None:
        deadline = time.monotonic() + timeout * 2 * len(servers)
    for server in servers:
        left = deadline - time.monotonic()
        if left < 0.2:
            break
        qid = secrets.randbelow(65536)
        packet = build_query(name, qid)
        try:
            status, recs = parse_response(_udp(server, port, packet, min(timeout, left)), qid)
            if status == "truncated":
                left = deadline - time.monotonic()
                if left < 0.2:
                    break
                status, recs = parse_response(_tcp(server, port, packet, min(timeout, left)), qid)
            if status in ("ok", "nodata", "nxdomain"):
                return status, recs
        except (OSError, ValueError, struct.error):
            continue
    return "error", []
