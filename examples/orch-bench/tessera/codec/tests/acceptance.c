/* Hidden acceptance for the C codec (R1, R2, R3, R4, R5, R6, R9). Built from the repository
 * root with:
 *   cc -std=c11 -Icodec/include -o codec/build/acceptance codec/tests/acceptance.c codec/build/libtessera.a
 */
#include "tessera.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int acc_failures;
static int acc_checks;

#define ACC_CHECK(cond, ...)                                                       \
  do {                                                                         \
    acc_checks++;                                                                  \
    if (!(cond)) {                                                             \
      acc_failures++;                                                              \
      fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__);                     \
      fprintf(stderr, __VA_ARGS__);                                            \
      fprintf(stderr, "\n");                                                   \
    }                                                                          \
  } while (0)

static size_t acc_unhex(const char *h, uint8_t *out) {
  size_t n = strlen(h) / 2;
  for (size_t i = 0; i < n; i++) {
    unsigned v;
    sscanf(h + 2 * i, "%2x", &v);
    out[i] = (uint8_t)v;
  }
  return n;
}

static tsr_slice acc_s(const char *s) {
  tsr_slice x = {(const uint8_t *)s, strlen(s)};
  return x;
}

static tsr_slice acc_sn(const void *p, size_t n) {
  tsr_slice x = {(const uint8_t *)p, n};
  return x;
}

static int acc_slice_eq(tsr_slice a, const char *s) {
  return a.len == strlen(s) && memcmp(a.ptr, s, a.len) == 0;
}

static tsr_record acc_base_record(void) {
  tsr_record r;
  memset(&r, 0, sizeof r);
  r.key = acc_s("k");
  r.kind = TSR_NULL;
  return r;
}

static void acc_test_strerror(void) {
  const struct { int code; const char *msg; } t[] = {
      {TSR_OK, "ok"},
      {TSR_ETRUNC, "truncated"},
      {TSR_EVARINT, "bad varint"},
      {TSR_ECRC, "checksum mismatch"},
      {TSR_EUTF8, "invalid utf-8"},
      {TSR_EKIND, "bad value kind"},
      {TSR_ETRAIL, "trailing bytes"},
      {TSR_EMAGIC, "bad magic"},
      {TSR_ELIMIT, "limit exceeded"},
      {TSR_EORDER, "unsorted tags"},
      {-100, "unknown error"},
      {7, "unknown error"},
  };
  for (size_t i = 0; i < sizeof t / sizeof t[0]; i++)
    ACC_CHECK(strcmp(tsr_strerror(t[i].code), t[i].msg) == 0, "strerror(%d) = %s", t[i].code,
          tsr_strerror(t[i].code));
}

static void acc_test_crc(void) {
  ACC_CHECK(tsr_crc32c((const uint8_t *)"", 0) == 0, "crc of empty");
  ACC_CHECK(tsr_crc32c((const uint8_t *)"123456789", 9) == 0xE3069283u, "crc check value");
  const char *fox = "The quick brown fox jumps over the lazy dog";
  ACC_CHECK(tsr_crc32c((const uint8_t *)fox, strlen(fox)) == 0x22620404u, "crc fox");
  uint8_t zeros[32] = {0};
  ACC_CHECK(tsr_crc32c(zeros, 32) == 0x8A9136AAu, "crc 32 zero bytes");
}

static void acc_test_varint(void) {
  const struct { uint64_t v; const char *hex; } t[] = {
      {0, "00"},
      {1, "01"},
      {127, "7f"},
      {128, "8001"},
      {300, "ac02"},
      {16383, "ff7f"},
      {16384, "808001"},
      {1048576, "808040"},
      {(uint64_t)1 << 63, "80808080808080808001"},
      {UINT64_MAX, "ffffffffffffffffff01"},
  };
  for (size_t i = 0; i < sizeof t / sizeof t[0]; i++) {
    uint8_t want[10], got[10] = {0};
    size_t wn = acc_unhex(t[i].hex, want);
    size_t gn = tsr_put_uvarint(got, t[i].v);
    ACC_CHECK(gn == wn && memcmp(got, want, wn) == 0, "put_uvarint(%llu)", (unsigned long long)t[i].v);
    uint64_t back = 0;
    ACC_CHECK(tsr_get_uvarint(want, wn, &back) == (int)wn && back == t[i].v, "get_uvarint(%s)", t[i].hex);
  }
  uint8_t buf[16];
  uint64_t v;
  size_t n = acc_unhex("ac02ff", buf);
  ACC_CHECK(tsr_get_uvarint(buf, n, &v) == 2 && v == 300, "get_uvarint stops at the final byte");
  ACC_CHECK(tsr_get_uvarint(buf, 0, &v) == TSR_ETRUNC, "empty input truncated");
  n = acc_unhex("80", buf);
  ACC_CHECK(tsr_get_uvarint(buf, n, &v) == TSR_ETRUNC, "80 truncated");
  n = acc_unhex("ac02", buf);
  ACC_CHECK(tsr_get_uvarint(buf, 1, &v) == TSR_ETRUNC, "len limit respected");
  n = acc_unhex("8000", buf);
  ACC_CHECK(tsr_get_uvarint(buf, n, &v) == TSR_EVARINT, "8000 not shortest");
  n = acc_unhex("ff00", buf);
  ACC_CHECK(tsr_get_uvarint(buf, n, &v) == TSR_EVARINT, "ff00 not shortest");
  n = acc_unhex("ffffffffffffffffff02", buf);
  ACC_CHECK(tsr_get_uvarint(buf, n, &v) == TSR_EVARINT, "10th byte 02 overflows");
  n = acc_unhex("ffffffffffffffffff81", buf);
  ACC_CHECK(tsr_get_uvarint(buf, n, &v) == TSR_EVARINT, "10th byte with high bit");
  n = acc_unhex("ffffffffffffffffffff01", buf);
  ACC_CHECK(tsr_get_uvarint(buf, n, &v) == TSR_EVARINT, "11 bytes");
  n = acc_unhex("ffffffffffffffffff", buf);
  ACC_CHECK(tsr_get_uvarint(buf, n, &v) == TSR_ETRUNC, "9 continuation bytes truncated");

  const struct { int64_t s; uint64_t z; } zz[] = {
      {0, 0}, {-1, 1}, {1, 2}, {-2, 3}, {2, 4}, {-64, 127}, {64, 128},
      {INT64_MAX, UINT64_MAX - 1}, {INT64_MIN, UINT64_MAX},
  };
  for (size_t i = 0; i < sizeof zz / sizeof zz[0]; i++) {
    ACC_CHECK(tsr_zigzag(zz[i].s) == zz[i].z, "zigzag(%lld)", (long long)zz[i].s);
    ACC_CHECK(tsr_unzigzag(zz[i].z) == zz[i].s, "unzigzag(%llu)", (unsigned long long)zz[i].z);
  }
}

/* key "k", ts -1, tags ["a"], int 300 */
static const char *ACC_GOLD_BODY = "016b0101016103d804";
static const char *ACC_GOLD_FRAME = "09016b0101016103d804f203f154";

static void acc_test_body(void) {
  uint8_t body[64];
  size_t n = acc_unhex(ACC_GOLD_BODY, body);
  tsr_record r;
  ACC_CHECK(tsr_decode_body(body, n, &r) == TSR_OK, "decode golden body");
  ACC_CHECK(acc_slice_eq(r.key, "k") && r.ts == -1 && r.ntags == 1 && acc_slice_eq(r.tags[0], "a") &&
            r.kind == TSR_INT && r.ival == 300,
        "golden body fields");
  ACC_CHECK(r.key.ptr == body + 1, "slices point into the body");

  tsr_record e = acc_base_record();
  e.ts = -1;
  e.ntags = 1;
  e.tags[0] = acc_s("a");
  e.kind = TSR_INT;
  e.ival = 300;
  uint8_t out[64];
  size_t len = 0;
  ACC_CHECK(tsr_encode_body(&e, out, sizeof out, &len) == TSR_OK && len == n && memcmp(out, body, n) == 0,
        "encode golden body");
  len = 0;
  ACC_CHECK(tsr_encode_body(&e, NULL, 0, &len) == TSR_ETRUNC && len == n, "encode with cap 0");
  len = 0;
  ACC_CHECK(tsr_encode_body(&e, out, n - 1, &len) == TSR_ETRUNC && len == n, "encode with small cap");

  uint8_t frame[64];
  size_t fn = acc_unhex(ACC_GOLD_FRAME, frame);
  len = 0;
  ACC_CHECK(tsr_append_frame(&e, out, sizeof out, &len) == TSR_OK && len == fn && memcmp(out, frame, fn) == 0,
        "append golden frame");
  len = 0;
  ACC_CHECK(tsr_append_frame(&e, out, fn - 1, &len) == TSR_ETRUNC && len == fn, "frame with small cap");
  len = 0;
  ACC_CHECK(tsr_append_frame(&e, NULL, 0, &len) == TSR_ETRUNC && len == fn, "frame with cap 0");

  /* each kind round-trips; unused fields are ignored when encoding */
  const char *bytes = "\x00\xff\x80";
  for (int k = 0; k <= 5; k++) {
    tsr_record x = acc_base_record();
    x.ts = INT64_MIN;
    x.kind = (tsr_kind)k;
    x.ival = k == TSR_INT ? INT64_MAX : 77;
    x.data = k == TSR_STRING ? acc_s("h\xc3\xa9llo") : acc_sn(bytes, 3);
    if (k != TSR_INT && k != TSR_STRING && k != TSR_BYTES) x.data = acc_s("ignored");
    size_t bl = 0;
    ACC_CHECK(tsr_encode_body(&x, out, sizeof out, &bl) == TSR_OK, "encode kind %d", k);
    tsr_record y;
    ACC_CHECK(tsr_decode_body(out, bl, &y) == TSR_OK, "decode kind %d", k);
    ACC_CHECK(y.kind == (tsr_kind)k && y.ts == INT64_MIN, "kind %d round trip", k);
    if (k == TSR_INT) ACC_CHECK(y.ival == INT64_MAX, "int value");
    else ACC_CHECK(y.ival == 0, "ival 0 for kind %d", k);
    if (k == TSR_STRING) ACC_CHECK(acc_slice_eq(y.data, "h\xc3\xa9llo"), "string value");
    else if (k == TSR_BYTES) ACC_CHECK(y.data.len == 3 && memcmp(y.data.ptr, bytes, 3) == 0, "bytes value");
    else ACC_CHECK(y.data.len == 0, "data len 0 for kind %d", k);
  }
  /* null kind: body is key, ts, tag count, kind byte only */
  tsr_record nr = acc_base_record();
  nr.data = acc_s("xyz");
  nr.ival = 5;
  ACC_CHECK(tsr_encode_body(&nr, out, sizeof out, &len) == TSR_OK && len == 5 &&
            memcmp(out, "\x01k\x00\x00\x00", 5) == 0,
        "null body ignores ival and data");
}

static void acc_test_encode_validation(void) {
  uint8_t out[1024];
  size_t len;
  char key256[257];
  memset(key256, 'k', 256);
  key256[256] = 0;
  char key255[256];
  memset(key255, 'k', 255);
  key255[255] = 0;
  tsr_record r = acc_base_record();
  r.key = acc_s("");
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_ELIMIT, "empty key");
  r.key = acc_s(key256);
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_ELIMIT, "256-byte key");
  r.key = acc_s(key255);
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_OK, "255-byte key");
  r.key = acc_s("\xc0\x80");
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_EUTF8, "overlong key");
  r.key = acc_s("\xed\xbf\xbf");
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_EUTF8, "surrogate key");
  r.key = acc_sn("a\0b", 3);
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_OK, "NUL inside key is valid");

  r = acc_base_record();
  r.ntags = 2;
  r.tags[0] = acc_s("b");
  r.tags[1] = acc_s("a");
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_EORDER, "unsorted tags");
  r.tags[0] = acc_s("a");
  r.tags[1] = acc_s("a");
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_EORDER, "duplicate tags");
  r.tags[0] = acc_s("a");
  r.tags[1] = acc_s("ab");
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_OK, "prefix tag sorts first");
  r.tags[1] = acc_s("");
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_ELIMIT, "empty tag");
  char t65[66];
  memset(t65, 't', 65);
  t65[65] = 0;
  r.tags[1] = acc_s(t65);
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_ELIMIT, "65-byte tag");
  r.tags[1] = acc_s("\xff");
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_EUTF8, "invalid tag");
  r.ntags = 65;
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_ELIMIT, "65 tags");

  r = acc_base_record();
  r.kind = (tsr_kind)6;
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_EKIND, "kind 6");
  r.kind = TSR_STRING;
  r.data = acc_s("\xf4\x90\x80\x80");
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_EUTF8, "string above U+10FFFF");
  r.kind = TSR_BYTES;
  ACC_CHECK(tsr_encode_body(&r, out, sizeof out, &len) == TSR_OK, "bytes may be anything");

  /* body limit: key "k" (2) + ts (1) + tag count (1) + kind (1) + len uvarint (3) + data */
  size_t big = 1048576 - 8;
  uint8_t *data = calloc(big + 1, 1);
  uint8_t *buf = malloc(1048576 + 16);
  r = acc_base_record();
  r.kind = TSR_BYTES;
  r.data = acc_sn(data, big);
  len = 0;
  ACC_CHECK(tsr_encode_body(&r, buf, 1048576 + 16, &len) == TSR_OK && len == 1048576, "body at the limit");
  r.data = acc_sn(data, big + 1);
  ACC_CHECK(tsr_encode_body(&r, buf, 1048576 + 16, &len) == TSR_ELIMIT, "body above the limit");
  ACC_CHECK(tsr_append_frame(&r, buf, 1048576 + 16, &len) == TSR_ELIMIT, "frame above the limit");
  free(data);
  free(buf);
}

static const struct { const char *name, *hex, *msg; } ACC_CORPUS[] = {
    {"empty file", "", "bad magic"},
    {"short magic", "545352", "bad magic"},
    {"wrong magic", "54535232", "bad magic"},
    {"len varint truncated", "5453523180", "record 0 at offset 4: truncated"},
    {"len varint not shortest", "545352318100", "record 0 at offset 4: bad varint"},
    {"len varint overflow", "54535231ffffffffffffffffff02", "record 0 at offset 4: bad varint"},
    {"len above limit", "54535231818040", "record 0 at offset 4: limit exceeded"},
    {"len at limit, body missing", "54535231808040016b", "record 0 at offset 4: truncated"},
    {"crc missing", "5453523105016b0000000000", "record 0 at offset 4: truncated"},
    {"crc mismatch", "5453523105016b0000002bb88695", "record 0 at offset 4: checksum mismatch"},
    {"empty body", "545352310000000000", "record 0 at offset 4: truncated"},
    {"key length 0", "545352310400000000c74b6748", "record 0 at offset 4: limit exceeded"},
    {"key length 256", "54535231850280026b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b00000065225c87", "record 0 at offset 4: limit exceeded"},
    {"key length 300 short body", "5453523104ac0261628fc8281c", "record 0 at offset 4: limit exceeded"},
    {"key truncated", "5453523103056162facb3848", "record 0 at offset 4: truncated"},
    {"key overlong utf-8", "545352310602c08000000084aaf587", "record 0 at offset 4: invalid utf-8"},
    {"key surrogate", "545352310703eda080000000c008aa8e", "record 0 at offset 4: invalid utf-8"},
    {"key invalid then truncated", "545352310201fff4bcbe4f", "record 0 at offset 4: invalid utf-8"},
    {"ts not shortest", "5453523106016b8000000030ba174b", "record 0 at offset 4: bad varint"},
    {"ts truncated", "5453523103016b805b44bc6e", "record 0 at offset 4: truncated"},
    {"tag count 65", "5453523104016b0041bbfa6cd2", "record 0 at offset 4: limit exceeded"},
    {"tag length 0", "5453523106016b0001000071e1f6bc", "record 0 at offset 4: limit exceeded"},
    {"tag length 65", "5453523147016b000141747474747474747474747474747474747474747474747474747474747474747474747474747474747474747474747474747474747474747474747474747474747400fd5f0d20", "record 0 at offset 4: limit exceeded"},
    {"tags unsorted", "5453523109016b000201620161007e1ba2d7", "record 0 at offset 4: unsorted tags"},
    {"tags duplicate", "5453523109016b00020161016100479280b5", "record 0 at offset 4: unsorted tags"},
    {"tags longer first", "545352310a016b0002026162016100a7657b58", "record 0 at offset 4: unsorted tags"},
    {"tag invalid utf-8", "5453523107016b0001018000391ab08f", "record 0 at offset 4: invalid utf-8"},
    {"tag missing", "5453523106016b0002016114a9d7d6", "record 0 at offset 4: truncated"},
    {"kind missing", "5453523104016b000004647c61", "record 0 at offset 4: truncated"},
    {"kind 6", "5453523105016b000006c25f27b3", "record 0 at offset 4: bad value kind"},
    {"int missing", "5453523105016b000003de4bd686", "record 0 at offset 4: truncated"},
    {"int overflow", "545352310f016b000003ffffffffffffffffff0322c54f32", "record 0 at offset 4: bad varint"},
    {"string above U+10FFFF", "545352310a016b00000404f4908080512be7de", "record 0 at offset 4: invalid utf-8"},
    {"string cut sequence", "5453523108016b00000402e28201044a7f", "record 0 at offset 4: invalid utf-8"},
    {"string length beyond body", "5453523109016b00000405616263c52aa9d1", "record 0 at offset 4: truncated"},
    {"bytes huge length", "5453523111016b000005808080808080808080016162534dd93d", "record 0 at offset 4: truncated"},
    {"trailing after null", "5453523106016b000000000f73b719", "record 0 at offset 4: trailing bytes"},
    {"trailing after true", "5453523106016b000002021633c9df", "record 0 at offset 4: trailing bytes"},
    {"second record crc", "5453523105016b0000002ab8869505016b0000002bb88695", "record 1 at offset 14: checksum mismatch"},
    {"third record truncated", "5453523105016b0000002ab8869505016b0000002ab8869507016b", "record 2 at offset 24: truncated"},
};

static void acc_message_for(const uint8_t *data, size_t len, char *msg, size_t cap, size_t *nrec) {
  tsr_reader r;
  tsr_record rec;
  *nrec = 0;
  if (tsr_reader_init(&r, data, len) != TSR_OK) {
    snprintf(msg, cap, "bad magic");
    return;
  }
  int e;
  while ((e = tsr_reader_next(&r, &rec)) == 1) (*nrec)++;
  if (e == 0) snprintf(msg, cap, "OK");
  else snprintf(msg, cap, "record %zu at offset %zu: %s", r.index, r.off, tsr_strerror(e));
}

static void acc_test_corpus(void) {
  static uint8_t buf[1 << 16];
  for (size_t i = 0; i < sizeof ACC_CORPUS / sizeof ACC_CORPUS[0]; i++) {
    size_t n = acc_unhex(ACC_CORPUS[i].hex, buf);
    char msg[256];
    size_t nrec;
    acc_message_for(buf, n, msg, sizeof msg, &nrec);
    ACC_CHECK(strcmp(msg, ACC_CORPUS[i].msg) == 0, "%s: got [%s] want [%s]", ACC_CORPUS[i].name, msg, ACC_CORPUS[i].msg);
  }
}

static void acc_test_reader(void) {
  uint8_t buf[256];
  size_t n = acc_unhex("54535231", buf);
  tsr_reader r;
  tsr_record rec;
  ACC_CHECK(tsr_reader_init(&r, buf, n) == TSR_OK && r.off == 4 && r.index == 0, "init magic only");
  ACC_CHECK(tsr_reader_next(&r, &rec) == 0, "magic only has no records");
  ACC_CHECK(tsr_reader_init(&r, buf, 3) == TSR_EMAGIC, "3 bytes");
  /* second frame: key "k", ts 0, no tags, null; crc 2ab88695 little-endian */
  n = acc_unhex("5453523109016b0101016103d804f203f15405016b0000002ab88695", buf);
  ACC_CHECK(tsr_reader_init(&r, buf, n) == TSR_OK, "init two records");
  ACC_CHECK(tsr_reader_next(&r, &rec) == 1 && rec.ival == 300 && r.off == 18 && r.index == 1, "first record");
  ACC_CHECK(tsr_reader_next(&r, &rec) == 1 && rec.kind == TSR_NULL && rec.ntags == 0 && r.off == 28 &&
            r.index == 2,
        "second record");
  ACC_CHECK(tsr_reader_next(&r, &rec) == 0 && r.off == 28, "end");
  ACC_CHECK(tsr_reader_next(&r, &rec) == 0, "end again");
  buf[n - 1] ^= 0xff;
  ACC_CHECK(tsr_reader_init(&r, buf, n) == TSR_OK, "init corrupt");
  ACC_CHECK(tsr_reader_next(&r, &rec) == 1, "first good");
  ACC_CHECK(tsr_reader_next(&r, &rec) == TSR_ECRC && r.off == 18 && r.index == 1, "second corrupt");
  ACC_CHECK(tsr_reader_next(&r, &rec) == TSR_ECRC && r.off == 18 && r.index == 1, "stays at failing frame");
}

static void acc_test_format(void) {
  char buf[512];
  tsr_record r = acc_base_record();
  r.key = acc_sn("a\"b\\c\n\r\t\x01\x1f\x7f/<&>\xc3\xa9\xf0\x9f\x98\x80\0z", 23);
  r.ts = INT64_MIN;
  r.ntags = 2;
  r.tags[0] = acc_s("x y");
  r.tags[1] = acc_s("\xe6\x97\xa5");
  r.kind = TSR_BYTES;
  r.data = acc_sn("\x00\xab\xff", 3);
  const char *want = "{\"key\":\"a\\\"b\\\\c\\n\\r\\t\\u0001\\u001f\\u007f/<&>\xc3\xa9\xf0\x9f\x98\x80\\u0000z\","
                     "\"ts\":-9223372036854775808,\"tags\":[\"x y\",\"\xe6\x97\xa5\"],"
                     "\"value\":{\"bytes\":\"00abff\"}}";
  size_t n = tsr_format_line(&r, buf, sizeof buf);
  ACC_CHECK(n == strlen(want) && strcmp(buf, want) == 0, "format line: got [%s]", buf);
  ACC_CHECK(tsr_format_line(&r, NULL, 0) == strlen(want), "format with cap 0");
  memset(buf, 'Z', sizeof buf);
  ACC_CHECK(tsr_format_line(&r, buf, 6) == strlen(want) && memcmp(buf, "{\"key\0", 6) == 0, "format truncates");
  ACC_CHECK(buf[6] == 'Z', "format writes at most cap bytes");

  const struct { tsr_kind kind; int64_t ival; const char *data; const char *want; } t[] = {
      {TSR_NULL, 0, "", "{\"key\":\"k\",\"ts\":0,\"tags\":[],\"value\":null}"},
      {TSR_FALSE, 0, "", "{\"key\":\"k\",\"ts\":0,\"tags\":[],\"value\":false}"},
      {TSR_TRUE, 0, "", "{\"key\":\"k\",\"ts\":0,\"tags\":[],\"value\":true}"},
      {TSR_INT, INT64_MAX, "", "{\"key\":\"k\",\"ts\":0,\"tags\":[],\"value\":9223372036854775807}"},
      {TSR_INT, -42, "", "{\"key\":\"k\",\"ts\":0,\"tags\":[],\"value\":-42}"},
      {TSR_STRING, 0, "", "{\"key\":\"k\",\"ts\":0,\"tags\":[],\"value\":\"\"}"},
      {TSR_STRING, 0, "\x1b[0m\x08\x0c", "{\"key\":\"k\",\"ts\":0,\"tags\":[],\"value\":\"\\u001b[0m\\u0008\\u000c\"}"},
      {TSR_BYTES, 0, "", "{\"key\":\"k\",\"ts\":0,\"tags\":[],\"value\":{\"bytes\":\"\"}}"},
  };
  for (size_t i = 0; i < sizeof t / sizeof t[0]; i++) {
    tsr_record x = acc_base_record();
    x.kind = t[i].kind;
    x.ival = t[i].ival;
    x.data = acc_s(t[i].data);
    tsr_format_line(&x, buf, sizeof buf);
    ACC_CHECK(strcmp(buf, t[i].want) == 0, "format kind %d: got [%s]", (int)t[i].kind, buf);
  }
}

int main(void) {
  acc_test_strerror();
  acc_test_crc();
  acc_test_varint();
  acc_test_body();
  acc_test_encode_validation();
  acc_test_corpus();
  acc_test_reader();
  acc_test_format();
  if (acc_failures) {
    fprintf(stderr, "codec acceptance: %d of %d checks failed\n", acc_failures, acc_checks);
    return 1;
  }
  printf("codec acceptance: %d checks passed\n", acc_checks);
  return 0;
}
