"""Builds a vbaProject.bin (the VBA part of an .xlsm) from module source code.

Implements just enough of [MS-OVBA] (VBA project storage) and [MS-CFB]
(compound file) to write a source-only project: no compiled p-code is stored,
so Excel compiles the modules from source the first time the file is opened.
"""
import random
import struct
import uuid

CODEPAGE = 1252
ENC = "cp1252"

WORKBOOK_BASE = "0{00020819-0000-0000-C000-000000000046}"
WORKSHEET_BASE = "0{00020820-0000-0000-C000-000000000046}"


# ---------------------------------------------------------------------------
# MS-OVBA 2.4.1 compression
# ---------------------------------------------------------------------------
def _copy_token_help(difference):
    bit_count = 4
    while (1 << bit_count) < difference:
        bit_count += 1
    length_mask = 0xFFFF >> bit_count
    return bit_count, length_mask, length_mask + 3


def compress(data: bytes) -> bytes:
    out = bytearray(b"\x01")
    pos = 0
    while pos < len(data):
        chunk_start = pos
        chunk_end = min(pos + 4096, len(data))
        header_at = len(out)
        out += b"\x00\x00"
        index = {}  # 3-byte prefix -> positions within this chunk

        def remember(p):
            if p + 3 <= chunk_end:
                index.setdefault(data[p:p + 3], []).append(p)

        while pos < chunk_end:
            flag_at = len(out)
            out.append(0)
            flags = 0
            for bit in range(8):
                if pos >= chunk_end:
                    break
                best_len, best_off = 0, 0
                if pos > chunk_start:
                    _, _, max_len = _copy_token_help(pos - chunk_start)
                    limit = min(max_len, chunk_end - pos)
                    for cand in reversed(index.get(data[pos:pos + 3], [])[-256:]):
                        n = 0
                        while n < limit and data[cand + n] == data[pos + n]:
                            n += 1
                        if n > best_len:
                            best_len, best_off = n, pos - cand
                            if n == limit:
                                break
                if best_len >= 3:
                    bit_count, _, _ = _copy_token_help(pos - chunk_start)
                    token = ((best_off - 1) << (16 - bit_count)) | (best_len - 3)
                    out += struct.pack("<H", token)
                    flags |= 1 << bit
                    for p in range(pos, pos + best_len):
                        remember(p)
                    pos += best_len
                else:
                    out.append(data[pos])
                    remember(pos)
                    pos += 1
            out[flag_at] = flags
        size = len(out) - header_at
        if size > 4098:
            raise ValueError("chunk did not compress; raw chunks are not supported")
        struct.pack_into("<H", out, header_at, 0xB000 | (size - 3))
    return bytes(out)


# ---------------------------------------------------------------------------
# MS-OVBA 2.4.3 data encryption (used for CMG / DPB / GC in PROJECT)
# ---------------------------------------------------------------------------
def _encrypt(project_id: str, data: bytes, rnd: random.Random) -> str:
    seed = rnd.randrange(256)
    version_enc = seed ^ 2
    proj_key = sum(project_id.encode(ENC)) & 0xFF
    proj_key_enc = seed ^ proj_key
    out = bytearray([seed, version_enc, proj_key_enc])
    unenc1, enc1, enc2 = proj_key, proj_key_enc, version_enc
    plain = bytes([0] * ((seed & 6) >> 1)) + struct.pack("<I", len(data)) + data
    for b in plain:
        e = b ^ ((enc2 + unenc1) & 0xFF)
        out.append(e)
        enc2, enc1, unenc1 = enc1, e, b
    return out.hex().upper()


# ---------------------------------------------------------------------------
# dir stream, PROJECT stream, PROJECTwm stream
# ---------------------------------------------------------------------------
def _rec(rec_id, data=b""):
    return struct.pack("<HI", rec_id, len(data)) + data


def _dir_stream(modules):
    d = bytearray()
    d += _rec(0x0001, struct.pack("<I", 1))            # SYSKIND: 32-bit Windows
    d += _rec(0x0002, struct.pack("<I", 0x409))        # LCID
    d += _rec(0x0014, struct.pack("<I", 0x409))        # LCIDINVOKE
    d += _rec(0x0003, struct.pack("<H", CODEPAGE))     # CODEPAGE
    d += _rec(0x0004, b"VBAProject")                   # NAME
    d += _rec(0x0005) + _rec(0x0040)                   # DOCSTRING (+ unicode)
    d += _rec(0x0006) + _rec(0x003D)                   # HELPFILEPATH 1 and 2
    d += _rec(0x0007, struct.pack("<I", 0))            # HELPCONTEXT
    d += _rec(0x0008, struct.pack("<I", 0))            # LIBFLAGS
    d += struct.pack("<HIIH", 0x0009, 4, 1, 0)         # VERSION (size field is fixed at 4)
    d += _rec(0x000C) + _rec(0x003C)                   # CONSTANTS (+ unicode)

    # Reference to OLE Automation (stdole), as Excel itself writes
    libid = b"*\\G{00020430-0000-0000-C000-000000000046}#2.0#0#C:\\Windows\\System32\\stdole2.tlb#OLE Automation"
    d += _rec(0x0016, b"stdole") + _rec(0x003E, "stdole".encode("utf-16-le"))
    d += _rec(0x000D, struct.pack("<I", len(libid)) + libid + struct.pack("<IH", 0, 0))

    d += _rec(0x000F, struct.pack("<H", len(modules)))  # MODULES count
    d += _rec(0x0013, struct.pack("<H", 0xFFFF))        # PROJECTCOOKIE
    for m in modules:
        name = m["name"]
        d += _rec(0x0019, name.encode(ENC))
        d += _rec(0x0047, name.encode("utf-16-le"))
        d += _rec(0x001A, name.encode(ENC)) + _rec(0x0032, name.encode("utf-16-le"))
        d += _rec(0x001C) + _rec(0x0048)                # DOCSTRING
        d += _rec(0x0031, struct.pack("<I", 0))         # OFFSET: source starts at 0
        d += _rec(0x001E, struct.pack("<I", 0))         # HELPCONTEXT
        d += _rec(0x002C, struct.pack("<H", 0xFFFF))    # COOKIE
        d += _rec(0x0022 if m["document"] else 0x0021)  # TYPE: document / procedural
        d += _rec(0x002B)                               # module terminator
    d += _rec(0x0010)                                   # dir terminator
    return bytes(d)


def _project_stream(modules, project_id, rnd):
    lines = [f'ID="{project_id}"']
    for m in modules:
        lines.append(f"Document={m['name']}/&H00000000" if m["document"] else f"Module={m['name']}")
    lines += [
        'Name="VBAProject"',
        'HelpContextID="0"',
        'VersionCompatible32="393222000"',
        'CMG="%s"' % _encrypt(project_id, struct.pack("<I", 0), rnd),   # not protected
        'DPB="%s"' % _encrypt(project_id, bytes([0x00]), rnd),         # no password
        'GC="%s"' % _encrypt(project_id, bytes([0xFF]), rnd),          # visible
        "",
        "[Host Extender Info]",
        "&H00000001={3832D640-CF90-11CF-8E43-00A0C911005A};VBE;&H00000000",
        "",
        "[Workspace]",
    ]
    for m in modules:
        lines.append(f"{m['name']}=0, 0, 0, 0, C" if m["document"] else f"{m['name']}=26, 26, 1200, 700, Z")
    return ("\r\n".join(lines) + "\r\n").encode(ENC)


def _projectwm_stream(modules):
    out = bytearray()
    for m in modules:
        out += m["name"].encode(ENC) + b"\x00" + m["name"].encode("utf-16-le") + b"\x00\x00"
    return bytes(out + b"\x00\x00")


def document_module(name, base):
    return "\r\n".join([
        f'Attribute VB_Name = "{name}"',
        f'Attribute VB_Base = "{base}"',
        "Attribute VB_GlobalNameSpace = False",
        "Attribute VB_Creatable = False",
        "Attribute VB_PredeclaredId = True",
        "Attribute VB_Exposed = True",
        "Attribute VB_TemplateDerived = False",
        "Attribute VB_Customizable = True",
        "",
    ])


# ---------------------------------------------------------------------------
# MS-CFB compound file writer (version 3, 512-byte sectors)
# ---------------------------------------------------------------------------
FREESECT, ENDOFCHAIN, FATSECT, NOSTREAM = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF
SECTOR, MINI, CUTOFF = 512, 64, 4096


class _Entry:
    def __init__(self, name, kind, data=b""):
        self.name, self.kind, self.data = name, kind, data   # kind: 1 storage, 2 stream, 5 root
        self.children = []
        self.left = self.right = self.child = NOSTREAM
        self.color = 1
        self.start, self.size = 0, 0


def _cfb_key(name):
    return (len(name), name.upper())


def _build_tree(entries, ids):
    """Balanced binary search tree of sibling entries, coloured as a valid red-black tree."""
    entries = sorted(entries, key=lambda e: _cfb_key(e.name))
    if not entries:
        return NOSTREAM
    depth_of = {}

    def build(lo, hi, depth):
        if lo > hi:
            return NOSTREAM
        mid = (lo + hi) // 2
        e = entries[mid]
        depth_of[id(e)] = depth
        e.left = build(lo, mid - 1, depth + 1)
        e.right = build(mid + 1, hi, depth + 1)
        return ids[id(e)]

    root = build(0, len(entries) - 1, 0)
    max_depth = max(depth_of.values())
    full = (1 << (max_depth + 1)) - 1 == len(entries)
    for e in entries:
        # the incomplete bottom level is red, everything else black
        e.color = 0 if (depth_of[id(e)] == max_depth and max_depth > 0 and not full) else 1
    return root


def write_cfb(root: _Entry) -> bytes:
    order = []

    def walk(e):
        order.append(e)
        for c in e.children:
            walk(c)

    walk(root)
    ids = {id(e): i for i, e in enumerate(order)}
    for e in order:
        if e.kind in (1, 5):
            e.child = _build_tree(e.children, ids)

    # Small streams go into the mini stream, large ones get their own sectors
    mini_data, mini_fat = bytearray(), []
    big = []
    for e in order:
        if e.kind != 2:
            continue
        e.size = len(e.data)
        if e.size == 0:
            e.start = ENDOFCHAIN
        elif e.size < CUTOFF:
            first = len(mini_data) // MINI
            n = -(-e.size // MINI)
            mini_data += e.data + b"\x00" * (n * MINI - e.size)
            mini_fat += [first + i + 1 for i in range(n - 1)] + [ENDOFCHAIN]
            e.start = first
        else:
            big.append(e)

    def sectors(nbytes):
        return -(-nbytes // SECTOR)

    n_dir = sectors(len(order) * 128)
    n_minifat = sectors(len(mini_fat) * 4)
    n_mini = sectors(len(mini_data))
    n_big = [sectors(len(e.data)) for e in big]
    n_other = n_dir + n_minifat + n_mini + sum(n_big)
    n_fat = 1
    while n_fat * 128 < n_fat + n_other:
        n_fat += 1
    if n_fat > 109:
        raise ValueError("file too large for this simple writer")

    fat = [FATSECT] * n_fat
    body = bytearray()

    def place(data, count):
        start = len(fat)
        fat.extend(start + i + 1 for i in range(count - 1))
        fat.append(ENDOFCHAIN)
        body.extend(data + b"\x00" * (count * SECTOR - len(data)))
        return start

    dir_start = len(fat)
    fat.extend(dir_start + i + 1 for i in range(n_dir - 1))
    fat.append(ENDOFCHAIN)
    dir_offset = len(body)
    body.extend(b"\x00" * (n_dir * SECTOR))          # directory written below
    minifat_start = place(struct.pack(f"<{len(mini_fat)}I", *mini_fat), n_minifat) if n_minifat else ENDOFCHAIN
    root.start = place(bytes(mini_data), n_mini) if n_mini else ENDOFCHAIN
    root.size = len(mini_data)
    for e, n in zip(big, n_big):
        e.start = place(e.data, n)
    fat += [FREESECT] * (n_fat * 128 - len(fat))

    dir_bytes = bytearray()
    for e in order:
        name = e.name.encode("utf-16-le")
        if len(name) > 62:
            raise ValueError("name too long: " + e.name)
        dir_bytes += name + b"\x00" * (64 - len(name))
        dir_bytes += struct.pack("<HBB", len(name) + 2, e.kind, e.color)
        dir_bytes += struct.pack("<III", e.left, e.right, e.child)
        dir_bytes += b"\x00" * 16 + struct.pack("<I", 0) + b"\x00" * 16
        dir_bytes += struct.pack("<IQ", e.start if e.kind != 1 else 0, e.size if e.kind != 1 else 0)
    while len(dir_bytes) < n_dir * SECTOR:
        dir_bytes += b"\x00" * 64 + struct.pack("<HBB", 0, 0, 0)
        dir_bytes += struct.pack("<III", NOSTREAM, NOSTREAM, NOSTREAM) + b"\x00" * 48
    body[dir_offset:dir_offset + len(dir_bytes)] = dir_bytes

    header = bytearray()
    header += bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 16
    header += struct.pack("<HHHHH", 0x003E, 0x0003, 0xFFFE, 9, 6) + b"\x00" * 6
    header += struct.pack("<IIIIIIIII", 0, n_fat, dir_start, 0, CUTOFF,
                          minifat_start, n_minifat, ENDOFCHAIN, 0)
    difat = list(range(n_fat)) + [FREESECT] * (109 - n_fat)
    header += struct.pack("<109I", *difat)
    assert len(header) == 512

    fat_bytes = struct.pack(f"<{len(fat)}I", *fat)
    return bytes(header) + fat_bytes + bytes(body)


# ---------------------------------------------------------------------------
def build_vba_project(standard_modules, workbook_name, sheet_names, seed=1):
    """standard_modules: list of (name, source); sheet_names: worksheet code names."""
    rnd = random.Random(seed)
    project_id = "{" + str(uuid.UUID(int=rnd.getrandbits(128))).upper() + "}"
    modules = [{"name": workbook_name, "document": True,
                "source": document_module(workbook_name, WORKBOOK_BASE)}]
    modules += [{"name": n, "document": True, "source": document_module(n, WORKSHEET_BASE)}
                for n in sheet_names]
    for name, src in standard_modules:
        src = src.replace("\r\n", "\n").replace("\n", "\r\n")
        if not src.startswith("Attribute VB_Name"):
            src = f'Attribute VB_Name = "{name}"\r\n' + src
        modules.append({"name": name, "document": False, "source": src})

    root = _Entry("Root Entry", 5)
    vba = _Entry("VBA", 1)
    vba.children.append(_Entry("_VBA_PROJECT", 2, struct.pack("<HHBH", 0x61CC, 0xFFFF, 0, 0)))
    vba.children.append(_Entry("dir", 2, compress(_dir_stream(modules))))
    for m in modules:
        vba.children.append(_Entry(m["name"], 2, compress(m["source"].encode(ENC))))
    root.children += [
        vba,
        _Entry("PROJECT", 2, _project_stream(modules, project_id, rnd)),
        _Entry("PROJECTwm", 2, _projectwm_stream(modules)),
    ]
    return write_cfb(root)
