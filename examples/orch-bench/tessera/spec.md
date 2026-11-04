# Tessera: a compact record archive in three languages

The repository holds three components, each in its own directory with its build files already
in place. Implement the requirements below and add tests in each component. Use only each
language's standard library: C11 with libc only, Go standard library only (no modules beyond
`tessera/cli` itself), and Ruby 3.2 standard library (minitest for tests).

- `codec/`: the C library `libtessera` and two command-line tools. `make -C codec` (the
  default target) builds `codec/build/libtessera.a`, `codec/build/tsr-dump` and
  `codec/build/tsr-filter` from sources under `codec/src/`; the public header is
  `codec/include/tessera.h`. A program must be able to use the library with
  `cc -std=c11 -Icodec/include prog.c codec/build/libtessera.a`.
- `cli/`: Go module `tessera/cli`. Package `tessera/cli/tsr` (directory `cli/tsr`) and the
  command `tsr`, built by `go build -o bin/tsr ./cmd/tsr` in `cli/`.
- `report/`: the Ruby library `report/lib/tessera.rb` (module `Tessera`, loadable with
  `require "tessera"` when `report/lib` is on the load path) and the command
  `ruby report/bin/tsr-report`, which must work from any working directory without `-I`.

"Byte order" below means comparing the UTF-8 (or raw) bytes lexicographically as unsigned
bytes, a shorter string that is a prefix of a longer one coming first.

## Shared format

R1. Varints. A *uvarint* is an unsigned 64-bit integer in LEB128: 7 bits per byte, least
significant group first, the high bit (0x80) set on every byte except the last. Encoders
always produce the shortest form (1 to 10 bytes). Decoders read one byte at a time and fail:
with `truncated` if the input ends before the final byte; with `bad varint` if the 10th byte is
greater than 0x01 (this covers both a set high bit and a value above 2^64 - 1); and with
`bad varint` if the encoding is not the shortest one (a final byte of 0x00 in a varint longer
than one byte). An *svarint* is a signed 64-bit integer n stored as the uvarint of its zigzag
form `(n << 1) ^ (n >> 63)` (arithmetic shift); so 0, -1, 1, -2 are stored as 0, 1, 2, 3, and
INT64_MIN as 2^64 - 1.

R2. Checksum. CRC-32C (Castagnoli): reflected polynomial 0x82F63B78, initial value
0xFFFFFFFF, final XOR 0xFFFFFFFF. The CRC of the empty input is 0 and of the ASCII bytes
`123456789` is 0xE3069283.

R3. Records. A record has a *key* (a UTF-8 string of 1 to 255 bytes), a *ts* (signed 64-bit
integer), *tags* (0 to 64 UTF-8 strings of 1 to 64 bytes each, in strictly ascending byte
order, so no duplicates) and a *value*, which is one of: null, false, true, an int (signed
64-bit), a string (UTF-8, 0 or more bytes) or bytes (any bytes, 0 or more). Valid UTF-8 is
well-formed UTF-8 as defined by Unicode: no overlong forms, no surrogates (U+D800 to U+DFFF),
nothing above U+10FFFF, no stray continuation bytes; U+0000 is allowed. The *body* of a record
is, in order: uvarint key length, key bytes; svarint ts; uvarint tag count, then per tag the
uvarint tag length and tag bytes; one *kind* byte (0 null, 1 false, 2 true, 3 int, 4 string,
5 bytes); then the payload: for int the svarint value, for string and bytes the uvarint length
and the bytes, nothing for the other kinds.

R4. Files. A Tessera file is the 4 magic bytes `54 53 52 31` (ASCII `TSR1`) followed by zero
or more frames. A frame is the uvarint body length L, the L body bytes, then the CRC-32C (R2)
of the body as 4 bytes little-endian. A body may be at most 1048576 bytes long; encoders
reject a record whose body would be longer (`TSR_ELIMIT` in C, an error in Go, `ArgumentError`
in Ruby). A file of exactly the magic bytes holds no records. Because every encoding is the
shortest one, the same records always encode to the same bytes.

R5. Decoding and errors. Decoders check the magic first: a file shorter than 4 bytes or with
other first bytes fails with the message `bad magic`. They then decode frames in order and stop
at the first failure, whose message is `record <i> at offset <o>: <reason>`, where i is the
number of records decoded before it (0-based index of the failing frame) and o is the byte
offset in the file of the first byte of that frame's length varint (decimal). The checks and
their reasons, in this exact order:
1. the body length L as a uvarint (`truncated`, `bad varint`);
2. L greater than 1048576: `limit exceeded`;
3. fewer than L + 4 bytes left after the length varint: `truncated`;
4. CRC-32C of the body differs from the stored CRC: `checksum mismatch`;
5. the body, field by field in R3 order, where running out of body bytes anywhere (inside a
   varint, a key, tag, string or bytes payload, or a missing kind byte) is `truncated`:
   key length uvarint, then a key length of 0 or above 255 is `limit exceeded`, then the key
   bytes, then key bytes that are not valid UTF-8 are `invalid utf-8`; the ts svarint; the tag
   count uvarint, then a count above 64 is `limit exceeded`; for each tag its length uvarint,
   a length of 0 or above 64 (`limit exceeded`), its bytes, invalid UTF-8 (`invalid utf-8`),
   and a tag not greater in byte order than the previous tag (`unsorted tags`); the kind byte,
   above 5 is `bad value kind`; the payload (a string payload that is not valid UTF-8 is
   `invalid utf-8`); finally, any bytes left in the body after the payload are
   `trailing bytes`. A length varint whose value exceeds the bytes remaining is `truncated`.

R6. Canonical line. The text form of a record is one line of JSON, exactly
`{"key":K,"ts":T,"tags":[G,...],"value":V}` with no spaces, where K and each G are JSON
strings, T is the decimal ts (a leading `-` when negative, no leading zeros, `0` for zero), the
tags keep their stored order (`[]` when there are none), and V is `null`, `false`, `true`, the
decimal int, a JSON string, or `{"bytes":"H"}` where H is the bytes in lowercase hex, two
digits per byte (`""` when empty). JSON strings are written between `"` with these and only
these escapes: `"` as `\"`, `\` as `\\`, LF as `\n`, CR as `\r`, TAB as `\t`, and every other
byte below 0x20 as well as 0x7F as `\u00XX` with two lowercase hex digits (for example
`\u0000`, `\u001b`, `\u007f`). All other characters, including `/`, `<`, `&` and every
non-ASCII character, are written as their raw UTF-8 bytes. A line that is printed is followed
by `\n`. Example: `{"key":"a\"b","ts":-5,"tags":["x","y"],"value":{"bytes":"00ff"}}`.

R7. Selection. Tools that select records take the options `--prefix P` (the key starts with
the bytes of P), `--tag T` (the record has tag T; may be repeated, and then every given tag is
required), `--since A` (ts >= A) and `--until B` (ts < B). A and B are decimal signed 64-bit
integers (an optional leading `-`, digits only); a record is selected when it satisfies every
option given. A missing option value, an unknown option, or an A or B that is not such an
integer is a usage error.

R8. Latest per key. Given a sequence of record lists (one per input file, in input order),
concatenate them in order. For each key, the *winner* is the record with the greatest ts;
among records with that ts, the one that comes last in the concatenation. The merge of the
lists is the winners sorted by key in byte order. Merging with *drop null* first picks the
winners and then omits winners whose value is null (so a null record hides older values of
its key).

## C codec (`codec/`)

R9. `codec/include/tessera.h` declares exactly this API (more declarations are allowed):

```c
#include <stddef.h>
#include <stdint.h>

#define TSR_MAX_TAGS 64

enum {
  TSR_OK = 0, TSR_ETRUNC = -1, TSR_EVARINT = -2, TSR_ECRC = -3, TSR_EUTF8 = -4,
  TSR_EKIND = -5, TSR_ETRAIL = -6, TSR_EMAGIC = -7, TSR_ELIMIT = -8, TSR_EORDER = -9
};

typedef enum {
  TSR_NULL = 0, TSR_FALSE = 1, TSR_TRUE = 2, TSR_INT = 3, TSR_STRING = 4, TSR_BYTES = 5
} tsr_kind;

typedef struct { const uint8_t *ptr; size_t len; } tsr_slice;

typedef struct {
  tsr_slice key;
  int64_t ts;
  size_t ntags;
  tsr_slice tags[TSR_MAX_TAGS];
  tsr_kind kind;
  int64_t ival;    /* the value when kind == TSR_INT, otherwise 0 after decoding */
  tsr_slice data;  /* the value when kind is TSR_STRING or TSR_BYTES, otherwise len 0 */
} tsr_record;

typedef struct { const uint8_t *data; size_t len; size_t off; size_t index; } tsr_reader;

const char *tsr_strerror(int code);
uint32_t tsr_crc32c(const uint8_t *data, size_t len);
size_t tsr_put_uvarint(uint8_t *buf, uint64_t v);
int tsr_get_uvarint(const uint8_t *buf, size_t len, uint64_t *out);
uint64_t tsr_zigzag(int64_t v);
int64_t tsr_unzigzag(uint64_t v);
int tsr_decode_body(const uint8_t *body, size_t len, tsr_record *rec);
int tsr_encode_body(const tsr_record *rec, uint8_t *buf, size_t cap, size_t *out_len);
int tsr_append_frame(const tsr_record *rec, uint8_t *buf, size_t cap, size_t *out_len);
int tsr_reader_init(tsr_reader *r, const uint8_t *data, size_t len);
int tsr_reader_next(tsr_reader *r, tsr_record *rec);
size_t tsr_format_line(const tsr_record *rec, char *buf, size_t cap);
```

Semantics:
- `tsr_strerror` returns the R5 reason for each error code (`truncated`, `bad varint`,
  `checksum mismatch`, `invalid utf-8`, `bad value kind`, `trailing bytes`, `bad magic`,
  `limit exceeded`, `unsorted tags`), `ok` for `TSR_OK` and `unknown error` for anything else.
- `tsr_put_uvarint` writes the shortest uvarint of v to buf (which has room for 10 bytes) and
  returns the number of bytes written. `tsr_get_uvarint` decodes a uvarint from the first
  `len` bytes of buf, stores it in `*out` and returns the number of bytes consumed (1 to 10),
  or returns `TSR_ETRUNC` / `TSR_EVARINT` per R1 (leaving `*out` unspecified).
  `tsr_zigzag`/`tsr_unzigzag` are R1's zigzag mapping and its inverse.
- `tsr_decode_body` decodes exactly `len` bytes of record body per R5 step 5 and returns
  `TSR_OK` or the error code of the first failed check. The slices in `rec` point into `body`.
- `tsr_encode_body` validates `rec` with R3's rules (key length `TSR_ELIMIT`, invalid UTF-8 in
  key, tag or string `TSR_EUTF8`, tag count or tag length `TSR_ELIMIT`, tag order
  `TSR_EORDER`, kind outside 0..5 `TSR_EKIND`) and encodes its body. It sets `*out_len` to
  the body's length; if that is greater than `cap` it returns `TSR_ETRUNC` (buf may then be
  NULL), otherwise it writes the body to buf and returns `TSR_OK`. Fields that the kind does
  not use are ignored. `tsr_append_frame` is the same for a whole frame (R4: length, body,
  CRC), with `*out_len` the frame's length.
- `tsr_reader_init` checks the magic (R5) and returns `TSR_OK` (setting `off` to 4 and
  `index` to 0) or `TSR_EMAGIC`. `tsr_reader_next` decodes the frame at `off`: it returns 1
  with the record in `*rec` and advances `off` past the frame and `index` by one; 0 when `off`
  is the end of the data; or a negative error code (R5 steps 1 to 5) leaving `off` and `index`
  at the failing frame, so that they are the `<o>` and `<i>` of the R5 message.
- `tsr_format_line` writes the R6 canonical line of rec, without a line ending, like
  `snprintf`: it returns the full length of the line, writes at most `cap - 1` of its bytes
  followed by a NUL when `cap > 0`, and writes nothing when `cap == 0` (buf may then be NULL).
  It assumes a valid record.

R10. `codec/build/tsr-dump FILE...` decodes each file in argument order and prints the
canonical line (R6) of every record. At the first failure it prints
`tsr-dump: <FILE>: <message>` (the R5 message) on stderr and exits with status 1, after the
lines of the records decoded before the failure and without processing further files. A file
that cannot be opened or read gives `tsr-dump: <FILE>: cannot read` and status 1. With no
arguments it prints a usage message on stderr and exits with status 2. Otherwise it exits 0.

R11. `codec/build/tsr-filter [options] IN OUT` with the R7 options (in any order, before IN
and OUT) reads the file IN and writes the file OUT holding the magic and, in their input
order, the frames (re-encoded with `tsr_append_frame`) of the records that are selected. It
then prints `kept <k> of <n>` (k selected out of n records) on stdout and exits 0. If IN cannot
be read (`tsr-filter: <IN>: cannot read`) or fails to decode (`tsr-filter: <IN>: <message>`)
it prints that on stderr, exits 1 and does not create OUT. A usage error (R7, or not exactly
two file arguments) exits 2 with a message on stderr starting with `tsr-filter: `.

## Go command line (`cli/`)

R12. Package `tessera/cli/tsr` provides:

```go
type Record struct {
	Key   string
	TS    int64
	Tags  []string
	Value any // nil, false/true (bool), int64, string or []byte
}

type FormatError struct {
	Index  int    // the <i> of R5
	Offset int    // the <o> of R5
	Reason string // the <reason> of R5, or "bad magic"
}

func (e *FormatError) Error() string // the R5 message: "bad magic" or "record <i> at offset <o>: <reason>"

func CRC32C(b []byte) uint32
func AppendUvarint(dst []byte, v uint64) []byte
func ReadUvarint(b []byte) (v uint64, n int, err error)
func Encode(records []Record) ([]byte, error)
func Decode(data []byte) ([]Record, error)
func FormatLine(r Record) string
func ParseLine(line string) (Record, error)
func Merge(inputs [][]Record, dropNull bool) []Record
```

`ReadUvarint` decodes a uvarint from the start of b and returns the value and the bytes used,
or an error whose `Error()` is `truncated` or `bad varint` (R1). `Encode` returns the whole
file (magic and frames) for the records, or an error if any record breaks R3 (tags are not
sorted for you; a Value of another Go type is an error). `Decode` returns the records of a
file; on failure it returns the records decoded before the failing frame and a
`*FormatError`. Records returned by `Decode` and `ParseLine` have a non-nil `Tags` slice
(empty when there are none), values of exactly the types listed above, and a non-nil
`[]byte` (possibly empty) for bytes. `FormatLine` returns the R6 line without a line ending.
`ParseLine` implements R13 and `Merge` implements R8, returning a new slice (possibly empty,
never nil).

R13. Input lines. `ParseLine` reads one JSON object (RFC 8259, with JSON whitespace allowed
around every token) with the members `"key"` (a string), `"ts"` (an integer), optionally
`"tags"` (an array of strings; absent means none) and optionally `"value"` (absent means
null): `null`, `true`, `false`, an integer, a string, or an object whose only member is
`"bytes"` with a string of hex digits (either case, even length) as its value. An integer is
a JSON number with no fraction and no exponent (`-?(0|[1-9][0-9]*)`) within the signed 64-bit
range. Strings use JSON's escapes (`\"`, `\\`, `\/`, `\b`, `\f`, `\n`, `\r`, `\t` and `\uXXXX`
in either case, where a high surrogate escape followed by a low surrogate escape is one
character). The tags are sorted in byte order and duplicates removed. It returns an error for
anything else, including: invalid JSON, content after the object, an unknown or duplicated
member, a missing `key` or `ts`, a member of the wrong type, a number with a fraction or
exponent or out of range, a raw control character (below 0x20) inside a string, raw bytes that
are not valid UTF-8, a `\u` escape of an unpaired surrogate, and a key, tag list (after
removing duplicates) or tag that breaks R3's limits.

R14. The `tsr` command (`cli/bin/tsr`). Every error message goes to stderr and starts with
`tsr: `. A file that cannot be read gives `tsr: <FILE>: cannot read` and status 1. An unknown
subcommand, an unknown flag or a missing argument exits 2.
- `tsr pack -o OUT [IN]` reads lines from IN, or stdin when IN is absent. Each line ends with
  `\n` (the final one may lack it; a `\r` before the `\n` stays in the line, where it is JSON
  whitespace). A line holding only JSON whitespace is skipped; every other line is a record
  (R13). It writes OUT with those records in input order (keys may repeat) and prints
  `packed <n> records` on stdout. On the first line that is not a valid record it prints
  `tsr: line <n>: invalid record` (n counted from 1, skipped lines included), exits 1 and does
  not create OUT.
- `tsr unpack FILE` prints the canonical line (R6) of every record. On a decode failure it
  prints `tsr: <FILE>: <message>` (R5) after the lines of the records before it and exits 1.
- `tsr verify FILE...` prints, on stdout, one line per file in argument order:
  `<FILE>: ok records=<n>` or `<FILE>: <message>` (R5; `cannot read` for an unreadable file).
  It checks every file and exits 1 if any failed, else 0.
- `tsr merge -o OUT [--drop-null] FILE...` decodes every FILE, writes OUT with the merge (R8)
  of their records in argument order, and prints `merged <n> records into <k>` (n records read
  in total, k written). If a FILE fails to decode it prints `tsr: <FILE>: <message>`, exits 1
  and does not create OUT.

## Ruby report (`report/`)

R15. `report/lib/tessera.rb` defines module `Tessera` with:
- `Tessera::Record`, a `Struct` with the members `key`, `ts`, `tags`, `value` (in this order;
  `Tessera::Record.new(key, ts, tags, value)`). Values are `nil`, `false`, `true`, an
  `Integer`, a `String` in binary (`ASCII-8BIT`) encoding (a bytes value) or a `String` in
  any other encoding (a string value). Decoding gives keys, tags and string values as UTF-8
  `String`s and bytes values as binary `String`s.
- `Tessera::FormatError < StandardError` whose `message` is the R5 message and which has
  readers `index`, `offset` (Integers, `nil` for `bad magic`) and `reason`.
- `Tessera.crc32c(str)` returns the R2 checksum of the string's bytes as an Integer.
- `Tessera.encode(records)` returns the file (magic and frames) as a binary String. A record
  that breaks R3 (including unsorted tags, an Integer ts or value outside the signed 64-bit
  range, a value of another class, or a string value, key or tag whose bytes are not valid
  UTF-8) raises `ArgumentError`. Keys and tags are taken as their bytes whatever their
  encoding.
- `Tessera.decode(str)` returns the Array of records of a file given as a String (any
  encoding; its bytes are used), raising `Tessera::FormatError` on the first failure (R5).
  `Tessera.read(path)` does the same for a file's contents.
- `Tessera.format_line(record)` returns the R6 line without a line ending, as a UTF-8 String.
- `Tessera.merge(lists, drop_null: false)` returns the R8 merge of an Array of record Arrays.

R16. `ruby report/bin/tsr-report [options] FILE...` takes the R7 options plus `--latest`.
It decodes the FILEs in argument order; with `--latest` it replaces the records by their
merge (R8, without dropping nulls) and then selects (R7). It prints exactly these lines on
stdout, where *matched* are the selected records:

```
files: <number of FILE arguments>
records: <records decoded from all files, before --latest>
matched: <number of matched records>
keys: <number of distinct keys among the matched records>
ts_min: <smallest ts among the matched records, or - when there are none>
ts_max: <largest ts, or ->
kinds: null=<n> false=<n> true=<n> int=<n> string=<n> bytes=<n>
int_sum: <exact sum of the int values, 0 when there are none; never overflows>
int_min: <smallest int value, or - when there are no int values>
int_max: <largest int value, or ->
value_bytes: <total byte length of the string and bytes values>
tag <tag> <count>
```

with one `tag` line per distinct tag of the matched records (count = matched records carrying
it), ordered by count descending and then by tag in byte order; no `tag` lines when there are
none. A file that cannot be read or decoded prints `tsr-report: <FILE>: cannot read` or
`tsr-report: <FILE>: <message>` (R5) on stderr, nothing on stdout, and exits 1. A usage error
(R7, an unknown option, or no FILE) exits 2 with a message on stderr starting with
`tsr-report: `.

## The components agree

R17. Encoders agree and decoders agree. For any valid records, `tsr pack` (from their R13
lines), `tsr.Encode`, `Tessera.encode` and a file of the magic followed by
`tsr_append_frame` frames produce byte-identical files; `tsr-dump`, `tsr unpack`,
`tsr.FormatLine` and `Tessera.format_line` print byte-identical lines for every valid file,
whichever component wrote it; and on any invalid file `tsr-dump`, `tsr unpack`/`tsr verify`,
`tsr.Decode` and `Tessera.decode` fail with the same R5 message. This includes keys of 255
bytes, 64 tags of 64 bytes, the extreme signed 64-bit values for ts and ints, empty strings
and bytes, non-ASCII text, U+0000 and other control characters.

R18. The tools compose. `tsr-filter` applied to a file written by `tsr pack` or
`Tessera.encode` yields exactly the magic followed by the original frames of the selected
records (byte for byte), and `tsr-report` on it reports the same lines as `tsr-report` with
the same selection options on the original file, except `files:` and `records:`. For the same
files in the same order, `tsr-report --latest` prints the same lines (except `files:` and
`records:`) as `tsr-report` on the output of `tsr merge` without `--drop-null`, and
`tsr merge` writes the same bytes as `Tessera.encode(Tessera.merge(...))`.
