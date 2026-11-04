// Hidden acceptance for package tessera/cli/tsr (R1, R2, R3, R4, R5, R6, R8, R12, R13).
// Run in cli/ with: go test ./tsr/ -run Acceptance -count=1
package tsr

import (
	"bytes"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"reflect"
	"strings"
	"testing"
)

func accMustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestAcceptanceCRC32C(t *testing.T) {
	cases := map[string]uint32{
		"":          0,
		"123456789": 0xE3069283,
		"The quick brown fox jumps over the lazy dog": 0x22620404,
		string(make([]byte, 32)):                      0x8A9136AA,
	}
	for in, want := range cases {
		if got := CRC32C([]byte(in)); got != want {
			t.Errorf("CRC32C(%q) = %08x, want %08x", in, got, want)
		}
	}
}

func TestAcceptanceUvarint(t *testing.T) {
	cases := []struct {
		v   uint64
		hex string
	}{
		{0, "00"}, {1, "01"}, {127, "7f"}, {128, "8001"}, {300, "ac02"}, {16383, "ff7f"},
		{16384, "808001"}, {1 << 63, "80808080808080808001"}, {math.MaxUint64, "ffffffffffffffffff01"},
	}
	for _, c := range cases {
		want := accMustHex(t, c.hex)
		if got := AppendUvarint([]byte{0xaa}, c.v); !bytes.Equal(got, append([]byte{0xaa}, want...)) {
			t.Errorf("AppendUvarint(%d) = %x", c.v, got)
		}
		v, n, err := ReadUvarint(append(want, 0x05))
		if err != nil || v != c.v || n != len(want) {
			t.Errorf("ReadUvarint(%s) = %d, %d, %v", c.hex, v, n, err)
		}
	}
	bad := map[string]string{
		"":                       "truncated",
		"80":                     "truncated",
		"ffffffffffffffffff":     "truncated",
		"8000":                   "bad varint",
		"ff8000":                 "bad varint",
		"ffffffffffffffffff02":   "bad varint",
		"ffffffffffffffffff81":   "bad varint",
		"ffffffffffffffffffff01": "bad varint",
	}
	for h, want := range bad {
		_, _, err := ReadUvarint(accMustHex(t, h))
		if err == nil || err.Error() != want {
			t.Errorf("ReadUvarint(%s) error = %v, want %s", h, err, want)
		}
	}
}

// key "k", ts -1, tags ["a"], int 300
const accGoldFile = "5453523109016b0101016103d804f203f154"

func TestAcceptanceEncodeGolden(t *testing.T) {
	got, err := Encode([]Record{{Key: "k", TS: -1, Tags: []string{"a"}, Value: int64(300)}})
	if err != nil || hex.EncodeToString(got) != accGoldFile {
		t.Fatalf("Encode golden = %x, %v", got, err)
	}
	empty, err := Encode(nil)
	if err != nil || string(empty) != "TSR1" {
		t.Fatalf("Encode(nil) = %q, %v", empty, err)
	}
	two, err := Encode([]Record{
		{Key: "k", TS: -1, Tags: []string{"a"}, Value: int64(300)},
		{Key: "k"},
	})
	if err != nil || hex.EncodeToString(two) != accGoldFile+"05016b0000002ab88695" {
		t.Fatalf("Encode two = %x, %v", two, err)
	}
}

func TestAcceptanceRoundTrip(t *testing.T) {
	tags64 := make([]string, 64)
	for i := range tags64 {
		tags64[i] = string(rune('A'+i/26)) + strings.Repeat(string(rune('a'+i%26)), 63)
	}
	recs := []Record{
		{Key: strings.Repeat("é", 127) + "x", TS: math.MinInt64, Tags: []string{}, Value: int64(math.MaxInt64)},
		{Key: "t", TS: math.MaxInt64, Tags: tags64, Value: int64(math.MinInt64)},
		{Key: "n\x00ul", TS: 0, Tags: []string{"a", "ab", "b"}, Value: nil},
		{Key: "f", TS: 1, Tags: []string{}, Value: false},
		{Key: "tr", TS: -1, Tags: []string{}, Value: true},
		{Key: "s", TS: 64, Tags: []string{"日本"}, Value: ""},
		{Key: "s2", TS: -65, Tags: []string{}, Value: "😀 \x00\x7f\"\\"},
		{Key: "b", TS: 1 << 62, Tags: []string{}, Value: []byte{}},
		{Key: "b2", TS: -(1 << 62), Tags: []string{}, Value: []byte{0, 0xff, 0x80}},
	}
	data, err := Encode(recs)
	if err != nil {
		t.Fatal(err)
	}
	back, err := Decode(data)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(back, recs) {
		t.Fatalf("round trip:\n got %#v\nwant %#v", back, recs)
	}
	if back[7].Value.([]byte) == nil || back[0].Tags == nil {
		t.Fatal("decoded empty bytes and tags must be non-nil")
	}
	again, _ := Encode(back)
	if !bytes.Equal(again, data) {
		t.Fatal("re-encoding changed bytes")
	}
	none, err := Decode([]byte("TSR1"))
	if err != nil || none == nil || len(none) != 0 {
		t.Fatalf("Decode(magic) = %#v, %v", none, err)
	}
}

func TestAcceptanceEncodeRejects(t *testing.T) {
	tags65 := make([]string, 65)
	for i := range tags65 {
		tags65[i] = string(rune('A' + i))
	}
	bad := map[string]Record{
		"empty key":       {Key: ""},
		"key 256":         {Key: strings.Repeat("k", 256)},
		"key utf8":        {Key: "\xff"},
		"key surrogate":   {Key: "\xed\xa0\x80"},
		"unsorted":        {Key: "k", Tags: []string{"b", "a"}},
		"duplicate":       {Key: "k", Tags: []string{"a", "a"}},
		"empty tag":       {Key: "k", Tags: []string{""}},
		"tag 65":          {Key: "k", Tags: []string{strings.Repeat("t", 65)}},
		"65 tags":         {Key: "k", Tags: tags65},
		"tag utf8":        {Key: "k", Tags: []string{"\xc0\x80"}},
		"string utf8":     {Key: "k", Value: "\xf4\x90\x80\x80"},
		"int type":        {Key: "k", Value: 5},
		"float":           {Key: "k", Value: 1.5},
		"body over limit": {Key: "k", Value: make([]byte, 1048576-8+1)},
	}
	for name, r := range bad {
		if _, err := Encode([]Record{{Key: "ok"}, r}); err == nil {
			t.Errorf("%s: Encode accepted an invalid record", name)
		}
	}
	atLimit := Record{Key: "k", Tags: []string{}, Value: make([]byte, 1048576-8)}
	data, err := Encode([]Record{atLimit})
	if err != nil {
		t.Fatalf("body at the limit rejected: %v", err)
	}
	if back, err := Decode(data); err != nil || len(back) != 1 || len(back[0].Value.([]byte)) != 1048568 {
		t.Fatalf("decode at limit: %v", err)
	}
}

var accCorpus = []struct{ name, hex, msg string }{
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
}

func TestAcceptanceDecodeErrors(t *testing.T) {
	for _, c := range accCorpus {
		recs, err := Decode(accMustHex(t, c.hex))
		var fe *FormatError
		if err == nil || !errors.As(err, &fe) {
			t.Errorf("%s: want *FormatError, got %v", c.name, err)
			continue
		}
		if err.Error() != c.msg {
			t.Errorf("%s: got %q want %q", c.name, err.Error(), c.msg)
		}
		if c.msg == "bad magic" {
			if fe.Reason != "bad magic" || len(recs) != 0 {
				t.Errorf("%s: fields %+v, %d records", c.name, fe, len(recs))
			}
			continue
		}
		reason := c.msg[strings.Index(c.msg, ": ")+2:]
		if fe.Reason != reason || fe.Index != len(recs) {
			t.Errorf("%s: fields %+v with %d records", c.name, fe, len(recs))
		}
	}
	// records before the failure are returned
	data := accMustHex(t, accGoldFile+"05016b0000002ab88694")
	recs, err := Decode(data)
	var fe *FormatError
	if !errors.As(err, &fe) || fe.Index != 1 || fe.Offset != 18 || fe.Reason != "checksum mismatch" || len(recs) != 1 || recs[0].Value != int64(300) {
		t.Fatalf("partial decode: %#v %v", recs, err)
	}
	if fe.Error() != "record 1 at offset 18: checksum mismatch" {
		t.Fatal(fe.Error())
	}
}

func TestAcceptanceFormatLine(t *testing.T) {
	cases := []struct {
		r    Record
		want string
	}{
		{Record{Key: "a\"b", TS: -5, Tags: []string{"x", "y"}, Value: []byte{0, 0xff}},
			`{"key":"a\"b","ts":-5,"tags":["x","y"],"value":{"bytes":"00ff"}}`},
		{Record{Key: "k", Tags: []string{}}, `{"key":"k","ts":0,"tags":[],"value":null}`},
		{Record{Key: "k", Tags: nil, Value: true}, `{"key":"k","ts":0,"tags":[],"value":true}`},
		{Record{Key: "k", Value: false}, `{"key":"k","ts":0,"tags":[],"value":false}`},
		{Record{Key: "k", TS: math.MaxInt64, Value: int64(math.MinInt64)},
			`{"key":"k","ts":9223372036854775807,"tags":[],"value":-9223372036854775808}`},
		{Record{Key: "\\/\n\r\t\b\f\x00\x1b\x7f<&> é😀", Value: ""},
			`{"key":"\\/\n\r\t\u0008\u000c\u0000\u001b\u007f<&>` + " é😀" + `","ts":0,"tags":[],"value":""}`},
		{Record{Key: "k", Value: []byte{}}, `{"key":"k","ts":0,"tags":[],"value":{"bytes":""}}`},
		{Record{Key: "k", Value: "line\nnext"}, `{"key":"k","ts":0,"tags":[],"value":"line\nnext"}`},
	}
	for _, c := range cases {
		if got := FormatLine(c.r); got != c.want {
			t.Errorf("FormatLine:\n got %s\nwant %s", got, c.want)
		}
	}
}

func TestAcceptanceParseLine(t *testing.T) {
	tags65 := make([]string, 65)
	for i := range tags65 {
		tags65[i] = fmt.Sprintf(`"t%02d"`, i)
	}
	tags64dup := append(append([]string{}, tags65[:64]...), `"t00"`)
	good := []struct {
		line string
		want Record
	}{
		{`{"key":"k","ts":0}`, Record{Key: "k", Tags: []string{}}},
		{" \t{ \"ts\" : -0 , \"key\" : \"k\" , \"value\" : null , \"tags\" : [ ] }\r", Record{Key: "k", Tags: []string{}}},
		{`{"key":"k","ts":-9223372036854775808,"value":9223372036854775807}`,
			Record{Key: "k", TS: math.MinInt64, Tags: []string{}, Value: int64(math.MaxInt64)}},
		{`{"key":"k","ts":1,"tags":["b","a","b","ab"],"value":true}`,
			Record{Key: "k", TS: 1, Tags: []string{"a", "ab", "b"}, Value: true}},
		{`{"key":"k","ts":1,"value":false}`, Record{Key: "k", TS: 1, Tags: []string{}, Value: false}},
		{`{"key":"é😀\/\"\\\b\f\n\r\t\u0000","ts":1,"value":"x"}`,
			Record{Key: "é😀/\"\\\b\f\n\r\t\x00", TS: 1, Tags: []string{}, Value: "x"}},
		{`{"key":"日本😀","ts":2,"value":""}`, Record{Key: "日本😀", TS: 2, Tags: []string{}, Value: ""}},
		{`{"key":"k","ts":3,"value":{"bytes":"DEADbeef"}}`,
			Record{Key: "k", TS: 3, Tags: []string{}, Value: []byte{0xde, 0xad, 0xbe, 0xef}}},
		{`{"key":"k","ts":3,"value":{ "bytes" : "" }}`, Record{Key: "k", TS: 3, Tags: []string{}, Value: []byte{}}},
		{`{"key":"` + strings.Repeat("é", 127) + `x","ts":4}`,
			Record{Key: strings.Repeat("é", 127) + "x", TS: 4, Tags: []string{}}},
		{`{"key":"k","ts":5,"tags":[` + strings.Join(tags64dup, ",") + `]}`, Record{Key: "k", TS: 5, Tags: func() []string {
			out := []string{}
			for _, s := range tags65[:64] {
				out = append(out, strings.Trim(s, `"`))
			}
			return out
		}()}},
	}
	for _, c := range good {
		got, err := ParseLine(c.line)
		if err != nil {
			t.Errorf("ParseLine(%q): %v", c.line, err)
			continue
		}
		if !reflect.DeepEqual(got, c.want) {
			t.Errorf("ParseLine(%q):\n got %#v\nwant %#v", c.line, got, c.want)
		}
	}
	bad := []string{
		``, `   `, `[]`, `null`, `"k"`, `{}`, `{"key":"a"}`, `{"ts":1}`,
		`{"key":"a","ts":1,"x":1}`, `{"key":"a","key":"b","ts":1}`, `{"key":"a","ts":1,"ts":2}`,
		`{"key":1,"ts":1}`, `{"key":"a","ts":"1"}`, `{"key":"a","ts":1.0}`, `{"key":"a","ts":1e3}`,
		`{"key":"a","ts":1E3}`, `{"key":"a","ts":9223372036854775808}`, `{"key":"a","ts":-9223372036854775809}`,
		`{"key":"a","ts":01}`, `{"key":"a","ts":+1}`, `{"key":"a","ts":-}`, `{"key":"a","ts":true}`,
		`{"key":"","ts":1}`, `{"key":"` + strings.Repeat("k", 256) + `","ts":1}`,
		`{"key":"a","ts":1,"tags":["a",1]}`, `{"key":"a","ts":1,"tags":"a"}`, `{"key":"a","ts":1,"tags":[""]}`,
		`{"key":"a","ts":1,"tags":["` + strings.Repeat("t", 65) + `"]}`,
		`{"key":"a","ts":1,"tags":[` + strings.Join(tags65, ",") + `]}`, `{"key":"a","ts":1,"tags":null}`,
		`{"key":"a","ts":1,"tags":["a",]}`, `{"key":"a","ts":1,"value":1.5}`, `{"key":"a","ts":1,"value":-0.0}`,
		`{"key":"a","ts":1,"value":9223372036854775808}`,
		`{"key":"a","ts":1,"value":{"bytes":"abc"}}`, `{"key":"a","ts":1,"value":{"bytes":"zz"}}`,
		`{"key":"a","ts":1,"value":{"bytes":"00","x":1}}`, `{"key":"a","ts":1,"value":{}}`,
		`{"key":"a","ts":1,"value":{"bytes":0}}`, `{"key":"a","ts":1,"value":{"Bytes":"00"}}`,
		`{"key":"a","ts":1,"value":[1]}`, `{"key":"a","ts":1} x`, `{"key":"a","ts":1}}`, `{"key":"a","ts":1},`,
		"{\"key\":\"a\tb\",\"ts\":1}", "{\"key\":\"a\nb\",\"ts\":1}", `{"key":"\ud800","ts":1}`,
		`{"key":"\udc00","ts":1}`, `{"key":"\ud800A","ts":1}`, `{"key":"\ud83d\ude0","ts":1}`,
		"{\"key\":\"\xff\",\"ts\":1}", "{\"key\":\"a\",\"ts\":1,\"value\":\"\xed\xa0\x80\"}",
		`{"key":"a","ts":1,"value":"\x"}`, `{"key":"a","ts":1,"value":"\u12"}`, `{"key":"a","ts":1,}`,
		`{"key":"a" "ts":1}`, `{"key":"a","ts":1,"value":nul}`, `{"key":"a","ts":1,"value":True}`,
		`{"key":"a","ts":1,"value":"unterminated}`, `{key:"a","ts":1}`, `{'key':'a','ts':1}`,
	}
	for _, line := range bad {
		if r, err := ParseLine(line); err == nil {
			t.Errorf("ParseLine(%q) accepted: %#v", line, r)
		}
	}
}

func TestAcceptanceMerge(t *testing.T) {
	a := []Record{
		{Key: "b", TS: 5, Tags: []string{}, Value: "a-b5"},
		{Key: "a", TS: 1, Tags: []string{}, Value: "a-a1"},
		{Key: "c", TS: 9, Tags: []string{}, Value: "a-c9"},
		{Key: "a", TS: 3, Tags: []string{}, Value: "a-a3"},
		{Key: "d", TS: 2, Tags: []string{}, Value: "a-d2-first"},
		{Key: "d", TS: 2, Tags: []string{}, Value: "a-d2-second"},
	}
	b := []Record{
		{Key: "b", TS: 5, Tags: []string{}, Value: "b-b5"},
		{Key: "a", TS: 2, Tags: []string{}, Value: "b-a2"},
		{Key: "c", TS: 10, Tags: []string{}, Value: nil},
		{Key: "é", TS: -1, Tags: []string{}, Value: int64(1)},
		{Key: "Z", TS: 0, Tags: []string{}, Value: nil},
	}
	got := Merge([][]Record{a, b}, false)
	want := []Record{
		{Key: "Z", TS: 0, Tags: []string{}, Value: nil},
		{Key: "a", TS: 3, Tags: []string{}, Value: "a-a3"},
		{Key: "b", TS: 5, Tags: []string{}, Value: "b-b5"},
		{Key: "c", TS: 10, Tags: []string{}, Value: nil},
		{Key: "d", TS: 2, Tags: []string{}, Value: "a-d2-second"},
		{Key: "é", TS: -1, Tags: []string{}, Value: int64(1)},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Merge:\n got %#v\nwant %#v", got, want)
	}
	got = Merge([][]Record{a, b}, true)
	want = []Record{want[1], want[2], want[4], want[5]}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Merge drop null:\n got %#v\nwant %#v", got, want)
	}
	got = Merge([][]Record{b, a}, false)
	if got[2].Value != "a-b5" {
		t.Fatalf("tie across inputs: later input wins, got %v", got[2].Value)
	}
	if m := Merge(nil, false); m == nil || len(m) != 0 {
		t.Fatalf("Merge(nil) = %#v", m)
	}
	if m := Merge([][]Record{{{Key: "x", Value: nil}}}, true); m == nil || len(m) != 0 {
		t.Fatalf("Merge drop all = %#v", m)
	}
	if a[0].Value != "a-b5" || len(a) != 6 {
		t.Fatal("Merge modified its input")
	}
}
