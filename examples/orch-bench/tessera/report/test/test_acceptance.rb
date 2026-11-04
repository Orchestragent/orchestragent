# frozen_string_literal: true

# Hidden acceptance for the Ruby library (R1-R6, R8, R15). From the repository root:
#   ruby -Ireport/lib report/test/test_acceptance.rb
require "minitest/autorun"
require "tempfile"
require "tessera"

class TesseraAcceptanceTest < Minitest::Test
  R = Tessera::Record
  GOLD = ["5453523109016b0101016103d804f203f154"].pack("H*") # key "k", ts -1, tags ["a"], int 300
  INT64_MIN = -(2**63)
  INT64_MAX = 2**63 - 1

  CORPUS = [
    ["empty file", "", "bad magic"],
    ["short magic", "545352", "bad magic"],
    ["wrong magic", "54535232", "bad magic"],
    ["len varint truncated", "5453523180", "record 0 at offset 4: truncated"],
    ["len varint not shortest", "545352318100", "record 0 at offset 4: bad varint"],
    ["len varint overflow", "54535231ffffffffffffffffff02", "record 0 at offset 4: bad varint"],
    ["len above limit", "54535231818040", "record 0 at offset 4: limit exceeded"],
    ["len at limit, body missing", "54535231808040016b", "record 0 at offset 4: truncated"],
    ["crc missing", "5453523105016b0000000000", "record 0 at offset 4: truncated"],
    ["crc mismatch", "5453523105016b0000002bb88695", "record 0 at offset 4: checksum mismatch"],
    ["empty body", "545352310000000000", "record 0 at offset 4: truncated"],
    ["key length 0", "545352310400000000c74b6748", "record 0 at offset 4: limit exceeded"],
    ["key length 256", "54535231850280026b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b00000065225c87", "record 0 at offset 4: limit exceeded"],
    ["key length 300 short body", "5453523104ac0261628fc8281c", "record 0 at offset 4: limit exceeded"],
    ["key truncated", "5453523103056162facb3848", "record 0 at offset 4: truncated"],
    ["key overlong utf-8", "545352310602c08000000084aaf587", "record 0 at offset 4: invalid utf-8"],
    ["key surrogate", "545352310703eda080000000c008aa8e", "record 0 at offset 4: invalid utf-8"],
    ["key invalid then truncated", "545352310201fff4bcbe4f", "record 0 at offset 4: invalid utf-8"],
    ["ts not shortest", "5453523106016b8000000030ba174b", "record 0 at offset 4: bad varint"],
    ["ts truncated", "5453523103016b805b44bc6e", "record 0 at offset 4: truncated"],
    ["tag count 65", "5453523104016b0041bbfa6cd2", "record 0 at offset 4: limit exceeded"],
    ["tag length 0", "5453523106016b0001000071e1f6bc", "record 0 at offset 4: limit exceeded"],
    ["tag length 65", "5453523147016b000141747474747474747474747474747474747474747474747474747474747474747474747474747474747474747474747474747474747474747474747474747474747400fd5f0d20", "record 0 at offset 4: limit exceeded"],
    ["tags unsorted", "5453523109016b000201620161007e1ba2d7", "record 0 at offset 4: unsorted tags"],
    ["tags duplicate", "5453523109016b00020161016100479280b5", "record 0 at offset 4: unsorted tags"],
    ["tags longer first", "545352310a016b0002026162016100a7657b58", "record 0 at offset 4: unsorted tags"],
    ["tag invalid utf-8", "5453523107016b0001018000391ab08f", "record 0 at offset 4: invalid utf-8"],
    ["tag missing", "5453523106016b0002016114a9d7d6", "record 0 at offset 4: truncated"],
    ["kind missing", "5453523104016b000004647c61", "record 0 at offset 4: truncated"],
    ["kind 6", "5453523105016b000006c25f27b3", "record 0 at offset 4: bad value kind"],
    ["int missing", "5453523105016b000003de4bd686", "record 0 at offset 4: truncated"],
    ["int overflow", "545352310f016b000003ffffffffffffffffff0322c54f32", "record 0 at offset 4: bad varint"],
    ["string above U+10FFFF", "545352310a016b00000404f4908080512be7de", "record 0 at offset 4: invalid utf-8"],
    ["string cut sequence", "5453523108016b00000402e28201044a7f", "record 0 at offset 4: invalid utf-8"],
    ["string length beyond body", "5453523109016b00000405616263c52aa9d1", "record 0 at offset 4: truncated"],
    ["bytes huge length", "5453523111016b000005808080808080808080016162534dd93d", "record 0 at offset 4: truncated"],
    ["trailing after null", "5453523106016b000000000f73b719", "record 0 at offset 4: trailing bytes"],
    ["trailing after true", "5453523106016b000002021633c9df", "record 0 at offset 4: trailing bytes"],
    ["second record crc", "5453523105016b0000002ab8869505016b0000002bb88695", "record 1 at offset 14: checksum mismatch"],
    ["third record truncated", "5453523105016b0000002ab8869505016b0000002ab8869507016b", "record 2 at offset 24: truncated"],
  ].freeze

  def b(str)
    str.dup.force_encoding(Encoding::BINARY)
  end

  def test_record_struct
    assert_equal %i[key ts tags value], R.members
    r = R.new("k", 1, ["a"], nil)
    assert_equal ["k", 1, ["a"], nil], [r.key, r.ts, r.tags, r.value]
    assert R < Struct
    assert Tessera::FormatError < StandardError
  end

  def test_crc32c
    assert_equal 0, Tessera.crc32c("")
    assert_equal 0xE3069283, Tessera.crc32c("123456789")
    assert_equal 0x22620404, Tessera.crc32c("The quick brown fox jumps over the lazy dog")
    assert_equal 0x8A9136AA, Tessera.crc32c("\x00".b * 32)
    assert_equal Tessera.crc32c("\xC3\xA9".b), Tessera.crc32c("é")
  end

  def test_encode_golden
    out = Tessera.encode([R.new("k", -1, ["a"], 300)])
    assert_equal Encoding::BINARY, out.encoding
    assert_equal GOLD, out
    assert_equal "TSR1".b, Tessera.encode([])
    two = Tessera.encode([R.new("k", -1, ["a"], 300), R.new("k", 0, [], nil)])
    assert_equal GOLD + ["05016b0000002ab88695"].pack("H*"), two
    assert_equal GOLD, Tessera.encode([R.new(b("k"), -1, [b("a")], 300)])
  end

  def test_varint_boundaries_in_bodies
    { 0 => "00", -1 => "01", 1 => "02", -2 => "03", 63 => "7e", -64 => "7f", 64 => "8001",
      INT64_MAX => "feffffffffffffffff01", INT64_MIN => "ffffffffffffffffff01" }.each do |ts, hex|
      body = ["016b"].pack("H*") + [hex].pack("H*") + "\x00\x00".b
      want = "TSR1".b + [body.bytesize].pack("C") + body + [Tessera.crc32c(body)].pack("V")
      assert_equal want, Tessera.encode([R.new("k", ts, [], nil)]), "ts #{ts}"
    end
  end

  def test_round_trip_and_types
    tags64 = (0...64).map { |i| format("t%02d", i) + ("x" * 61) }
    recs = [
      R.new(("é" * 127) + "x", INT64_MIN, [], INT64_MAX),
      R.new("t", INT64_MAX, tags64, INT64_MIN),
      R.new("n\u0000ul", 0, %w[a ab b], nil),
      R.new("f", 1, [], false),
      R.new("tr", -1, [], true),
      R.new("s", 64, ["日本"], ""),
      R.new("s2", -65, [], "😀 \u0000\u007f\"\\"),
      R.new("b", 2**62, [], "".b),
      R.new("b2", -(2**62), [], "\x00\xff\x80".b),
    ]
    data = Tessera.encode(recs)
    back = Tessera.decode(data)
    assert_equal recs, back
    back.each do |r|
      assert_equal Encoding::UTF_8, r.key.encoding
      r.tags.each { |t| assert_equal Encoding::UTF_8, t.encoding }
    end
    assert_equal Encoding::UTF_8, back[5].value.encoding
    assert_equal Encoding::UTF_8, back[6].value.encoding
    assert_equal Encoding::BINARY, back[7].value.encoding
    assert_equal Encoding::BINARY, back[8].value.encoding
    assert_equal [], back[0].tags
    assert_equal data, Tessera.encode(back)
    assert_equal back, Tessera.decode(data.dup.force_encoding(Encoding::UTF_8))
    assert_equal [], Tessera.decode("TSR1")
    Tempfile.create(["tessera", ".tsr"]) do |f|
      f.binmode
      f.write(data)
      f.close
      assert_equal recs, Tessera.read(f.path)
    end
  end

  def test_value_classes
    ascii = "abc".encode(Encoding::US_ASCII)
    assert_equal Tessera.encode([R.new("k", 0, [], "abc")]), Tessera.encode([R.new("k", 0, [], ascii)])
    latin = "caf\xE9".b.force_encoding(Encoding::ISO_8859_1)
    assert_raises(ArgumentError) { Tessera.encode([R.new("k", 0, [], latin)]) }
  end

  def test_encode_rejects
    bad = {
      "empty key" => R.new("", 0, [], nil),
      "key 256" => R.new("k" * 256, 0, [], nil),
      "key utf8" => R.new("\xff".b, 0, [], nil),
      "key utf8 tagged utf-8" => R.new(+"\xff", 0, [], nil),
      "unsorted" => R.new("k", 0, %w[b a], nil),
      "duplicate" => R.new("k", 0, %w[a a], nil),
      "prefix order" => R.new("k", 0, %w[ab a], nil),
      "empty tag" => R.new("k", 0, [""], nil),
      "tag 65" => R.new("k", 0, ["t" * 65], nil),
      "65 tags" => R.new("k", 0, (0...65).map { |i| format("t%02d", i) }, nil),
      "tag utf8" => R.new("k", 0, ["\xc0\x80".b], nil),
      "string utf8" => R.new("k", 0, [], +"\xf4\x90\x80\x80"),
      "ts too big" => R.new("k", 2**63, [], nil),
      "ts too small" => R.new("k", -(2**63) - 1, [], nil),
      "int too big" => R.new("k", 0, [], 2**63),
      "int too small" => R.new("k", 0, [], -(2**63) - 1),
      "float" => R.new("k", 0, [], 1.5),
      "symbol" => R.new("k", 0, [], :sym),
      "body over limit" => R.new("k", 0, [], "\x00".b * (1_048_576 - 8 + 1)),
    }
    bad.each do |name, rec|
      assert_raises(ArgumentError, name) { Tessera.encode([R.new("ok", 0, [], nil), rec]) }
    end
    at_limit = Tessera.encode([R.new("k", 0, [], "\x00".b * (1_048_576 - 8))])
    assert_equal 1_048_568, Tessera.decode(at_limit)[0].value.bytesize
  end

  def test_decode_errors
    CORPUS.each do |name, hex, msg|
      err = assert_raises(Tessera::FormatError, name) { Tessera.decode([hex].pack("H*")) }
      assert_equal msg, err.message, name
      if msg == "bad magic"
        assert_nil err.index, name
        assert_nil err.offset, name
        assert_equal "bad magic", err.reason, name
      else
        m = msg.match(/\Arecord (\d+) at offset (\d+): (.*)\z/)
        assert_equal [m[1].to_i, m[2].to_i, m[3]], [err.index, err.offset, err.reason], name
      end
    end
  end

  def test_format_line
    cases = [
      [R.new("a\"b", -5, %w[x y], "\x00\xff".b), '{"key":"a\"b","ts":-5,"tags":["x","y"],"value":{"bytes":"00ff"}}'],
      [R.new("k", 0, [], nil), '{"key":"k","ts":0,"tags":[],"value":null}'],
      [R.new("k", 0, [], true), '{"key":"k","ts":0,"tags":[],"value":true}'],
      [R.new("k", 0, [], false), '{"key":"k","ts":0,"tags":[],"value":false}'],
      [R.new("k", INT64_MAX, [], INT64_MIN), '{"key":"k","ts":9223372036854775807,"tags":[],"value":-9223372036854775808}'],
      [R.new("\\/\n\r\t\b\f\u0000\e\u007f<&> é😀", 0, [], ""),
       "{\"key\":\"\\\\/\\n\\r\\t\\u0008\\u000c\\u0000\\u001b\\u007f<&> é😀\",\"ts\":0,\"tags\":[],\"value\":\"\"}"],
      [R.new("k", 0, [], "".b), '{"key":"k","ts":0,"tags":[],"value":{"bytes":""}}'],
      [R.new("k", 0, ["日本"], "line\nnext"), '{"key":"k","ts":0,"tags":["日本"],"value":"line\nnext"}'],
    ]
    cases.each do |rec, want|
      got = Tessera.format_line(rec)
      assert_equal Encoding::UTF_8, got.encoding
      assert_equal want, got
    end
  end

  def test_merge
    a = [
      R.new("b", 5, [], "a-b5"), R.new("a", 1, [], "a-a1"), R.new("c", 9, [], "a-c9"),
      R.new("a", 3, [], "a-a3"), R.new("d", 2, [], "a-d2-first"), R.new("d", 2, [], "a-d2-second"),
    ]
    bl = [
      R.new("b", 5, [], "b-b5"), R.new("a", 2, [], "b-a2"), R.new("c", 10, [], nil),
      R.new("é", -1, [], 1), R.new("Z", 0, [], nil),
    ]
    want = [
      R.new("Z", 0, [], nil), R.new("a", 3, [], "a-a3"), R.new("b", 5, [], "b-b5"),
      R.new("c", 10, [], nil), R.new("d", 2, [], "a-d2-second"), R.new("é", -1, [], 1),
    ]
    assert_equal want, Tessera.merge([a, bl])
    assert_equal want.values_at(1, 2, 4, 5), Tessera.merge([a, bl], drop_null: true)
    assert_equal "a-b5", Tessera.merge([bl, a])[2].value
    assert_equal [], Tessera.merge([])
    assert_equal [], Tessera.merge([[R.new("x", 0, [], nil)]], drop_null: true)
    assert_equal 6, a.size
  end
end
