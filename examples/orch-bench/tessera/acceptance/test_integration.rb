# frozen_string_literal: true

# Hidden acceptance: the three components together (R5-R8, R10, R11, R14, R16, R17, R18).
# From the repository root, after building the C tools and the Go command:
#   ruby acceptance/test_integration.rb
#
# The Go command packs JSON lines; the C tools dump and filter the files; the Ruby library
# encodes the same records and must produce the same bytes; all decoders must agree on the
# canonical text and on every error message; tsr-report must agree with tsr merge and
# tsr-filter.
require "minitest/autorun"
require "open3"
require "rbconfig"
require "tmpdir"
require "fileutils"

ROOT = File.expand_path("..", __dir__)
$LOAD_PATH.unshift(File.join(ROOT, "report", "lib"))
require "tessera"

DUMP = File.join(ROOT, "codec", "build", "tsr-dump")
FILTER = File.join(ROOT, "codec", "build", "tsr-filter")
TSR = File.join(ROOT, "cli", "bin", "tsr")
REPORT = File.join(ROOT, "report", "bin", "tsr-report")

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
KEY255 = ("é" * 127) + "k"
TAGS64 = (0...64).map { |i| format("tag%02d", i).ljust(64, "x") }

R = Tessera::Record

# Input lines for `tsr pack`, with blank lines, JSON whitespace, a CRLF line and no final newline.
PACK_INPUT = [
  '{"key":"alpha","ts":10,"tags":["red","blue"],"value":42}',
  '{"key":"beta","ts":-3,"value":"hello, world"}',
  "",
  "  \t ",
  '{"key":"gamma","ts":0,"tags":["red"],"value":true}',
  '{"key":"delta","ts":9223372036854775807,"value":-9223372036854775808}',
  '{"key":"epsilon","ts":-9223372036854775808,"value":9223372036854775807}',
  '{"key":"zeta","ts":5,"value":{"bytes":"DEADbeef00"}}',
  '{"key":"eta","ts":6,"value":{"bytes":""}}',
  '{"key":"theta","ts":7,"value":""}',
  '{"key":"iota","ts":8,"value":null,"tags":["blue","blue"]}',
  '{"key":"kappa","ts":9,"value":false}',
  '{"key":"ünïcödé ключ 日本","ts":11,"tags":["日本","é"],"value":"😀 \ud83d\ude00 ' + "\u2028" + ' </script> & \/"}',
  '{"key":"ctl\u0000\u0001\u001f\u007f\b\f\n\r\t\"\\\\","ts":12,"value":"tab\there"}',
  "{\"key\":\"#{KEY255}\",\"ts\":13,\"tags\":[#{TAGS64.reverse.map { |t| "\"#{t}\"" }.join(',')}]}",
  "  {\"value\" : 1 , \"ts\" : 14 , \"key\" : \"spaced\" }  \r",
  '{"key":"alpha","ts":10,"tags":["green"],"value":43}',
  '{"key":"alpha","ts":4,"value":1}',
].join("\n")

RECORDS = [
  R.new("alpha", 10, %w[blue red], 42),
  R.new("beta", -3, [], "hello, world"),
  R.new("gamma", 0, ["red"], true),
  R.new("delta", INT64_MAX, [], INT64_MIN),
  R.new("epsilon", INT64_MIN, [], INT64_MAX),
  R.new("zeta", 5, [], "\xde\xad\xbe\xef\x00".b),
  R.new("eta", 6, [], "".b),
  R.new("theta", 7, [], ""),
  R.new("iota", 8, ["blue"], nil),
  R.new("kappa", 9, [], false),
  R.new("ünïcödé ключ 日本", 11, ["é", "日本"], "😀 😀 \u2028 </script> & /"),
  R.new("ctl\u0000\u0001\u001f\u007f\b\f\n\r\t\"\\", 12, [], "tab\there"),
  R.new(KEY255, 13, TAGS64, nil),
  R.new("spaced", 14, [], 1),
  R.new("alpha", 10, ["green"], 43),
  R.new("alpha", 4, [], 1),
].freeze

EXPECTED_LINES = [
  '{"key":"alpha","ts":10,"tags":["blue","red"],"value":42}',
  '{"key":"beta","ts":-3,"tags":[],"value":"hello, world"}',
  '{"key":"gamma","ts":0,"tags":["red"],"value":true}',
  '{"key":"delta","ts":9223372036854775807,"tags":[],"value":-9223372036854775808}',
  '{"key":"epsilon","ts":-9223372036854775808,"tags":[],"value":9223372036854775807}',
  '{"key":"zeta","ts":5,"tags":[],"value":{"bytes":"deadbeef00"}}',
  '{"key":"eta","ts":6,"tags":[],"value":{"bytes":""}}',
  '{"key":"theta","ts":7,"tags":[],"value":""}',
  '{"key":"iota","ts":8,"tags":["blue"],"value":null}',
  '{"key":"kappa","ts":9,"tags":[],"value":false}',
  '{"key":"ünïcödé ключ 日本","ts":11,"tags":["é","日本"],"value":"😀 😀 ' + "\u2028" + ' </script> & /"}',
  '{"key":"ctl\u0000\u0001\u001f\u007f\u0008\u000c\n\r\t\"\\\\","ts":12,"tags":[],"value":"tab\there"}',
  "{\"key\":\"#{KEY255}\",\"ts\":13,\"tags\":[#{TAGS64.map { |t| "\"#{t}\"" }.join(',')}],\"value\":null}",
  '{"key":"spaced","ts":14,"tags":[],"value":1}',
  '{"key":"alpha","ts":10,"tags":["green"],"value":43}',
  '{"key":"alpha","ts":4,"tags":[],"value":1}',
].freeze
EXPECTED_TEXT = EXPECTED_LINES.map { |l| "#{l}\n" }.join.b

REPORT_ALL = [
  "files: 1",
  "records: 16",
  "matched: 16",
  "keys: 14",
  "ts_min: -9223372036854775808",
  "ts_max: 9223372036854775807",
  "kinds: null=2 false=1 true=1 int=6 string=4 bytes=2",
  "int_sum: 86",
  "int_min: -9223372036854775808",
  "int_max: 9223372036854775807",
  "value_bytes: 52",
  "tag blue 2",
  "tag red 2",
  "tag green 1",
  *TAGS64.map { |t| "tag #{t} 1" },
  "tag é 1",
  "tag 日本 1",
].freeze

# Two small files for the report and merge checks.
FILE1 = [
  R.new("user:1", 5, %w[b c], 10),
  R.new("user:2", 7, ["c"], INT64_MAX),
  R.new("user:1", 9, %w[a c], INT64_MAX),
  R.new("item:é", 3, %w[Z é], "naïve"),
].freeze
FILE2 = [
  R.new("user:2", 7, %w[a b], nil),
  R.new("item:x", -4, [], "\x01\x02\x03".b),
  R.new("user:3", 1, ["b"], false),
  R.new("user:1", 2, [], true),
].freeze

# Invalid files (hex) and the R5 message every decoder must give.
CORPUS = [
  ["empty file", "", "bad magic"],
  ["wrong magic", "54535232", "bad magic"],
  ["len varint truncated", "5453523180", "record 0 at offset 4: truncated"],
  ["len varint not shortest", "545352318100", "record 0 at offset 4: bad varint"],
  ["len varint overflow", "54535231ffffffffffffffffff02", "record 0 at offset 4: bad varint"],
  ["len above limit", "54535231818040", "record 0 at offset 4: limit exceeded"],
  ["len at limit, body missing", "54535231808040016b", "record 0 at offset 4: truncated"],
  ["crc missing", "5453523105016b0000000000", "record 0 at offset 4: truncated"],
  ["crc mismatch", "5453523105016b0000002bb88695", "record 0 at offset 4: checksum mismatch"],
  ["empty body", "545352310000000000", "record 0 at offset 4: truncated"],
  ["key length 300 short body", "5453523104ac0261628fc8281c", "record 0 at offset 4: limit exceeded"],
  ["key surrogate", "545352310703eda080000000c008aa8e", "record 0 at offset 4: invalid utf-8"],
  ["ts not shortest", "5453523106016b8000000030ba174b", "record 0 at offset 4: bad varint"],
  ["tag count 65", "5453523104016b0041bbfa6cd2", "record 0 at offset 4: limit exceeded"],
  ["tags unsorted", "5453523109016b000201620161007e1ba2d7", "record 0 at offset 4: unsorted tags"],
  ["tags longer first", "545352310a016b0002026162016100a7657b58", "record 0 at offset 4: unsorted tags"],
  ["tag invalid utf-8", "5453523107016b0001018000391ab08f", "record 0 at offset 4: invalid utf-8"],
  ["kind 6", "5453523105016b000006c25f27b3", "record 0 at offset 4: bad value kind"],
  ["int overflow", "545352310f016b000003ffffffffffffffffff0322c54f32", "record 0 at offset 4: bad varint"],
  ["string above U+10FFFF", "545352310a016b00000404f4908080512be7de", "record 0 at offset 4: invalid utf-8"],
  ["bytes huge length", "5453523111016b000005808080808080808080016162534dd93d", "record 0 at offset 4: truncated"],
  ["trailing after null", "5453523106016b000000000f73b719", "record 0 at offset 4: trailing bytes"],
  ["second record crc", "5453523105016b0000002ab8869505016b0000002bb88695", "record 1 at offset 14: checksum mismatch"],
  ["third record truncated", "5453523105016b0000002ab8869505016b0000002ab8869507016b", "record 2 at offset 24: truncated"],
].freeze

class TesseraIntegrationTest < Minitest::Test
  def setup
    @dir = Dir.mktmpdir("tessera-acceptance")
  end

  def teardown
    FileUtils.rm_rf(@dir)
  end

  def path(name)
    File.join(@dir, name)
  end

  def run_cmd(*cmd, stdin: "")
    out, err, st = Open3.capture3(*cmd, stdin_data: stdin, chdir: @dir, binmode: true)
    [out.b, err.b, st.exitstatus]
  end

  def report(*args)
    run_cmd(RbConfig.ruby, REPORT, *args)
  end

  def write(name, data)
    File.binwrite(path(name), data)
    path(name)
  end

  def packed
    input = write("input.jsonl", PACK_INPUT)
    out, err, st = run_cmd(TSR, "pack", "-o", path("packed.tsr"), input)
    assert_equal [0, "packed 16 records\n", ""], [st, out, err]
    File.binread(path("packed.tsr"))
  end

  def frames_of(records)
    records.map { |r| Tessera.encode([r]).byteslice(4..) }.join.b
  end

  def lines(text)
    text.force_encoding(Encoding::UTF_8).split("\n")
  end

  # R17, R14 pack: the Go packer and the Ruby encoder write the same bytes.
  def test_pack_and_ruby_encode_agree
    data = packed
    assert_equal Tessera.encode(RECORDS), data
    out, err, st = run_cmd(TSR, "pack", "-o", path("stdin.tsr"), stdin: PACK_INPUT)
    assert_equal [0, "packed 16 records\n", ""], [st, out, err]
    assert_equal data, File.binread(path("stdin.tsr"))
  end

  # R17, R10, R14 unpack: tsr-dump, tsr unpack and Tessera.format_line print the same text.
  def test_dump_unpack_and_format_line_agree
    file = write("p.tsr", packed)
    out, err, st = run_cmd(DUMP, file)
    assert_equal [0, ""], [st, err]
    assert_equal EXPECTED_TEXT, out
    out, err, st = run_cmd(TSR, "unpack", file)
    assert_equal [0, ""], [st, err]
    assert_equal EXPECTED_TEXT, out
    rb = Tessera.read(file).map { |r| "#{Tessera.format_line(r)}\n" }.join.b
    assert_equal EXPECTED_TEXT, rb
    assert_equal RECORDS, Tessera.read(file)
    ruby_file = write("ruby.tsr", Tessera.encode(RECORDS))
    assert_equal EXPECTED_TEXT, run_cmd(DUMP, ruby_file)[0]
    assert_equal EXPECTED_TEXT, run_cmd(TSR, "unpack", ruby_file)[0]
    out, _, st = run_cmd(TSR, "verify", file, ruby_file)
    assert_equal [0, "#{file}: ok records=16\n#{ruby_file}: ok records=16\n"], [st, out]
  end

  # R11, R17: the C encoder re-encodes every record to the same bytes.
  def test_filter_reencodes_identically
    file = write("p.tsr", packed)
    out, err, st = run_cmd(FILTER, file, path("all.tsr"))
    assert_equal [0, "kept 16 of 16\n", ""], [st, out, err]
    assert_equal File.binread(file), File.binread(path("all.tsr"))
    empty = write("empty.tsr", "TSR1")
    out, _, st = run_cmd(FILTER, empty, path("e.tsr"))
    assert_equal [0, "kept 0 of 0\n"], [st, out]
    assert_equal "TSR1", File.binread(path("e.tsr"))
  end

  # R5, R17: every decoder fails with the same message on every invalid file.
  def test_errors_agree
    CORPUS.each do |name, hex, msg|
      file = write("bad.tsr", [hex].pack("H*"))
      out, err, st = run_cmd(DUMP, file)
      assert_equal [1, "tsr-dump: #{file}: #{msg}\n"], [st, err], "tsr-dump #{name}"
      unpack_out, err, st = run_cmd(TSR, "unpack", file)
      assert_equal [1, "tsr: #{file}: #{msg}\n"], [st, err], "tsr unpack #{name}"
      assert_equal out, unpack_out, "partial output #{name}"
      vout, _, st = run_cmd(TSR, "verify", file)
      assert_equal [1, "#{file}: #{msg}\n"], [st, vout], "tsr verify #{name}"
      e = assert_raises(Tessera::FormatError, name) { Tessera.read(file) }
      assert_equal msg, e.message, "Tessera.read #{name}"
      rout, err, st = report(file)
      assert_equal [1, "", "tsr-report: #{file}: #{msg}\n"], [st, rout, err], "tsr-report #{name}"
      _, err, st = run_cmd(FILTER, file, path("never.tsr"))
      assert_equal [1, "tsr-filter: #{file}: #{msg}\n"], [st, err], "tsr-filter #{name}"
      refute File.exist?(path("never.tsr")), "tsr-filter created OUT for #{name}"
      _, err, st = run_cmd(TSR, "merge", "-o", path("never.tsr"), file)
      assert_equal [1, "tsr: #{file}: #{msg}\n"], [st, err], "tsr merge #{name}"
      refute File.exist?(path("never.tsr")), "tsr merge created OUT for #{name}"
    end
    file = write("partial.tsr", ["5453523105016b0000002ab8869505016b0000002ab8869507016b"].pack("H*"))
    line = "{\"key\":\"k\",\"ts\":0,\"tags\":[],\"value\":null}\n"
    assert_equal line * 2, run_cmd(DUMP, file)[0]
    assert_equal line * 2, run_cmd(TSR, "unpack", file)[0]
  end

  # R10: several files, stopping at the first failure, usage and unreadable files.
  def test_dump_files
    a = write("a.tsr", Tessera.encode(FILE1))
    b = write("b.tsr", Tessera.encode(FILE2))
    bad = write("bad.tsr", ["5453523105016b0000002ab8869505016b0000002bb88695"].pack("H*"))
    out, err, st = run_cmd(DUMP, a, b)
    want = (FILE1 + FILE2).map { |r| "#{Tessera.format_line(r)}\n" }.join.b
    assert_equal [0, want, ""], [st, out, err]
    out, err, st = run_cmd(DUMP, a, bad, b)
    a_text = FILE1.map { |r| "#{Tessera.format_line(r)}\n" }.join.b
    assert_equal [1, "#{a_text}{\"key\":\"k\",\"ts\":0,\"tags\":[],\"value\":null}\n",
                  "tsr-dump: #{bad}: record 1 at offset 14: checksum mismatch\n"], [st, out, err]
    _, err, st = run_cmd(DUMP)
    assert_equal 2, st
    refute_empty err
    out, err, st = run_cmd(DUMP, a, path("missing.tsr"))
    assert_equal [1, a_text, "tsr-dump: #{path('missing.tsr')}: cannot read\n"], [st, out, err]
    assert_equal [0, "", ""], run_cmd(DUMP, write("m.tsr", "TSR1")).values_at(2, 0, 1)
  end

  # R7, R11: selection options and errors of tsr-filter.
  def test_filter_options
    file = write("p.tsr", packed)
    cases = {
      %w[--prefix al] => RECORDS.select { |r| r.key.start_with?("al") },
      %w[--tag red --tag blue] => [RECORDS[0]],
      %w[--tag blue] => [RECORDS[0], RECORDS[8]],
      %w[--since 5 --until 10] => RECORDS.select { |r| r.ts >= 5 && r.ts < 10 },
      %w[--until 0] => RECORDS.select { |r| r.ts.negative? },
      ["--since", "-9223372036854775808"] => RECORDS,
      ["--until", "-9223372036854775808"] => [],
      ["--prefix", "ünï"] => [RECORDS[10]],
      ["--prefix", "", "--tag", TAGS64[63]] => [RECORDS[12]],
      ["--until", "11", "--prefix", "alpha", "--since", "-3", "--tag", "green"] => [RECORDS[14]],
    }
    cases.each do |opts, want|
      out_file = path("f.tsr")
      FileUtils.rm_f(out_file)
      out, err, st = run_cmd(FILTER, *opts, file, out_file)
      assert_equal [0, "kept #{want.size} of 16\n", ""], [st, out, err], opts.inspect
      assert_equal "TSR1".b + frames_of(want), File.binread(out_file), opts.inspect
    end
    [
      %w[--since 1.5], %w[--since abc], %w[--until 9223372036854775808], %w[--since +1],
      %w[--bogus x], ["--prefix"],
    ].each do |opts|
      _, err, st = run_cmd(FILTER, *opts, file, path("u.tsr"))
      assert_equal 2, st, opts.inspect
      assert err.start_with?("tsr-filter: "), "#{opts.inspect}: #{err}"
      refute File.exist?(path("u.tsr")), opts.inspect
    end
    [[file], [file, path("u.tsr"), path("v.tsr")], []].each do |args|
      _, err, st = run_cmd(FILTER, *args)
      assert_equal 2, st, args.inspect
      assert err.start_with?("tsr-filter: "), err
    end
    _, err, st = run_cmd(FILTER, path("missing.tsr"), path("u.tsr"))
    assert_equal [1, "tsr-filter: #{path('missing.tsr')}: cannot read\n"], [st, err]
    refute File.exist?(path("u.tsr"))
  end

  # R13, R14: pack line handling and errors.
  def test_pack_lines_and_errors
    out, err, st = run_cmd(TSR, "pack", "-o", path("x.tsr"),
                           stdin: "{\"key\":\"a\",\"ts\":1}\r\n\n{\"key\":\"b\",\"ts\":2}\n")
    assert_equal [0, "packed 2 records\n", ""], [st, out, err]
    assert_equal Tessera.encode([R.new("a", 1, [], nil), R.new("b", 2, [], nil)]), File.binread(path("x.tsr"))
    out, _, st = run_cmd(TSR, "pack", "-o", path("blank.tsr"), stdin: "\n  \n\t\r\n")
    assert_equal [0, "packed 0 records\n"], [st, out]
    assert_equal "TSR1", File.binread(path("blank.tsr"))
    out, _, st = run_cmd(TSR, "pack", "-o", path("none.tsr"), stdin: "")
    assert_equal [0, "packed 0 records\n"], [st, out]
    {
      "{\"key\":\"a\",\"ts\":1}\n\n{\"key\":\"b\"}\n" => 3,
      "{\"key\":\"a\",\"ts\":1.5}" => 1,
      "{\"key\":\"a\",\"ts\":1}\n{\"key\":\"a\",\"ts\":1,\"extra\":true}\n" => 2,
      "\n\n\n{\"key\":\"\",\"ts\":1}\n" => 4,
      "{\"key\":\"a\",\"ts\":1}\n{\"key\":\"a\",\"ts\":1} trailing\n" => 2,
      "{\"key\":\"\\ud800\",\"ts\":1}\n" => 1,
      "{\"key\":\"a\",\"ts\":1,\"value\":{\"bytes\":\"0\"}}\n" => 1,
      "{\"key\":\"a\",\"ts\":1,\"tags\":[\"\"]}\n" => 1,
      "{\"key\":\"a\xff\",\"ts\":1}\n".b => 1,
    }.each do |input, line|
      FileUtils.rm_f(path("bad.tsr"))
      out, err, st = run_cmd(TSR, "pack", "-o", path("bad.tsr"), stdin: input)
      assert_equal [1, "", "tsr: line #{line}: invalid record\n"], [st, out, err], input.inspect
      refute File.exist?(path("bad.tsr")), input.inspect
    end
    _, err, st = run_cmd(TSR, "pack", "-o", path("y.tsr"), path("missing.jsonl"))
    assert_equal [1, "tsr: #{path('missing.jsonl')}: cannot read\n"], [st, err]
    [%w[pack], %w[frobnicate], [], %w[pack --bogus -o x.tsr], %w[unpack], %w[verify], %w[merge a.tsr]].each do |args|
      _, err, st = run_cmd(TSR, *args)
      assert_equal 2, st, args.inspect
      assert err.start_with?("tsr: "), "#{args.inspect}: #{err}"
    end
    _, err, st = run_cmd(TSR, "unpack", path("missing.tsr"))
    assert_equal [1, "tsr: #{path('missing.tsr')}: cannot read\n"], [st, err]
  end

  # R14 verify.
  def test_verify
    good = write("good.tsr", packed)
    empty = write("empty.tsr", "TSR1")
    bad = write("bad.tsr", "TSR1\x05\x01k\x00\x00\x06".b)
    out, err, st = run_cmd(TSR, "verify", good, empty)
    assert_equal [0, "#{good}: ok records=16\n#{empty}: ok records=0\n", ""], [st, out, err]
    out, _, st = run_cmd(TSR, "verify", bad, path("missing.tsr"), good)
    assert_equal 1, st
    assert_equal "#{bad}: record 0 at offset 4: truncated\n#{path('missing.tsr')}: cannot read\n" \
                 "#{good}: ok records=16\n", out
  end

  # R8, R14 merge, R18: tsr merge agrees with Tessera.merge and Tessera.encode.
  def test_merge
    a = write("a.tsr", Tessera.encode(FILE1))
    b = write("b.tsr", Tessera.encode(FILE2))
    out, err, st = run_cmd(TSR, "merge", "-o", path("m.tsr"), a, b)
    assert_equal [0, "merged 8 records into 5\n", ""], [st, out, err]
    merged = Tessera.decode(File.binread(path("m.tsr")))
    assert_equal [
      R.new("item:x", -4, [], "\x01\x02\x03".b),
      R.new("item:é", 3, %w[Z é], "naïve"),
      R.new("user:1", 9, %w[a c], INT64_MAX),
      R.new("user:2", 7, %w[a b], nil),
      R.new("user:3", 1, ["b"], false),
    ], merged
    assert_equal Tessera.encode(Tessera.merge([FILE1, FILE2])), File.binread(path("m.tsr"))
    out, _, st = run_cmd(TSR, "merge", "-o", path("d.tsr"), "--drop-null", a, b)
    assert_equal [0, "merged 8 records into 4\n"], [st, out]
    assert_equal Tessera.encode(Tessera.merge([FILE1, FILE2], drop_null: true)), File.binread(path("d.tsr"))
    run_cmd(TSR, "merge", "-o", path("r.tsr"), b, a)
    reversed = Tessera.read(path("r.tsr"))
    assert_equal INT64_MAX, reversed.find { |r| r.key == "user:2" }.value
    p = write("p.tsr", packed)
    out, _, st = run_cmd(TSR, "merge", "-o", path("pm.tsr"), p)
    assert_equal [0, "merged 16 records into 14\n"], [st, out]
    pm = Tessera.read(path("pm.tsr"))
    assert_equal pm.map { |r| r.key.b }.sort, pm.map { |r| r.key.b }
    assert_equal R.new("alpha", 10, ["green"], 43), pm.find { |r| r.key == "alpha" }
    assert_equal Tessera.encode(Tessera.merge([RECORDS])), File.binread(path("pm.tsr"))
    out, _, = run_cmd(DUMP, path("pm.tsr"))
    assert_equal Tessera.merge([RECORDS]).map { |r| "#{Tessera.format_line(r)}\n" }.join.b, out
  end

  # R16: exact report output.
  def test_report_corpus
    file = write("p.tsr", packed)
    out, err, st = report(file)
    assert_equal [0, ""], [st, err]
    assert_equal REPORT_ALL, lines(out)
    assert out.end_with?("\n")
  end

  def test_report_two_files
    a = write("a.tsr", Tessera.encode(FILE1))
    b = write("b.tsr", Tessera.encode(FILE2))
    expectations = {
      [] => [
        "files: 2", "records: 8", "matched: 8", "keys: 5", "ts_min: -4", "ts_max: 9",
        "kinds: null=1 false=1 true=1 int=3 string=1 bytes=1", "int_sum: 18446744073709551624",
        "int_min: 10", "int_max: 9223372036854775807", "value_bytes: 9",
        "tag b 3", "tag c 3", "tag a 2", "tag Z 1", "tag é 1",
      ],
      %w[--latest] => [
        "files: 2", "records: 8", "matched: 5", "keys: 5", "ts_min: -4", "ts_max: 9",
        "kinds: null=1 false=1 true=0 int=1 string=1 bytes=1", "int_sum: 9223372036854775807",
        "int_min: 9223372036854775807", "int_max: 9223372036854775807", "value_bytes: 9",
        "tag a 2", "tag b 2", "tag Z 1", "tag c 1", "tag é 1",
      ],
      %w[--prefix user: --tag c] => [
        "files: 2", "records: 8", "matched: 3", "keys: 2", "ts_min: 5", "ts_max: 9",
        "kinds: null=0 false=0 true=0 int=3 string=0 bytes=0", "int_sum: 18446744073709551624",
        "int_min: 10", "int_max: 9223372036854775807", "value_bytes: 0",
        "tag c 3", "tag a 1", "tag b 1",
      ],
      %w[--tag a --tag b] => [
        "files: 2", "records: 8", "matched: 1", "keys: 1", "ts_min: 7", "ts_max: 7",
        "kinds: null=1 false=0 true=0 int=0 string=0 bytes=0", "int_sum: 0",
        "int_min: -", "int_max: -", "value_bytes: 0", "tag a 1", "tag b 1",
      ],
      %w[--since 3 --until 9] => [
        "files: 2", "records: 8", "matched: 4", "keys: 3", "ts_min: 3", "ts_max: 7",
        "kinds: null=1 false=0 true=0 int=2 string=1 bytes=0", "int_sum: 9223372036854775817",
        "int_min: 10", "int_max: 9223372036854775807", "value_bytes: 6",
        "tag b 2", "tag c 2", "tag Z 1", "tag a 1", "tag é 1",
      ],
      %w[--prefix nothing] => [
        "files: 2", "records: 8", "matched: 0", "keys: 0", "ts_min: -", "ts_max: -",
        "kinds: null=0 false=0 true=0 int=0 string=0 bytes=0", "int_sum: 0",
        "int_min: -", "int_max: -", "value_bytes: 0",
      ],
      ["--prefix", "item:é"] => [
        "files: 2", "records: 8", "matched: 1", "keys: 1", "ts_min: 3", "ts_max: 3",
        "kinds: null=0 false=0 true=0 int=0 string=1 bytes=0", "int_sum: 0",
        "int_min: -", "int_max: -", "value_bytes: 6", "tag Z 1", "tag é 1",
      ],
      %w[--latest --since 0] => [
        "files: 2", "records: 8", "matched: 4", "keys: 4", "ts_min: 1", "ts_max: 9",
        "kinds: null=1 false=1 true=0 int=1 string=1 bytes=0", "int_sum: 9223372036854775807",
        "int_min: 9223372036854775807", "int_max: 9223372036854775807", "value_bytes: 6",
        "tag a 2", "tag b 2", "tag Z 1", "tag c 1", "tag é 1",
      ],
    }
    expectations.each do |opts, want|
      out, err, st = report(*opts, a, b)
      assert_equal [0, ""], [st, err], opts.inspect
      assert_equal want, lines(out), opts.inspect
    end
    out, _, st = report("--since", "-4", a, b)
    assert_equal [0, "matched: 8"], [st, lines(out)[2]]
    out, _, st = report(write("e.tsr", "TSR1"))
    assert_equal 0, st
    assert_equal ["files: 1", "records: 0", "matched: 0", "keys: 0", "ts_min: -", "ts_max: -",
                  "kinds: null=0 false=0 true=0 int=0 string=0 bytes=0", "int_sum: 0",
                  "int_min: -", "int_max: -", "value_bytes: 0"], lines(out)
    out, _, = report(a, a, b)
    assert_equal ["files: 3", "records: 12", "matched: 12", "keys: 5"], lines(out)[0, 4]
  end

  def test_report_usage_errors
    a = write("a.tsr", Tessera.encode(FILE1))
    [%w[--since abc], %w[--since 1.5], %w[--until 9223372036854775808], %w[--bogus], %w[--tag]].each do |opts|
      args = opts == %w[--tag] ? [a, "--tag"] : [*opts, a]
      out, err, st = report(*args)
      assert_equal [2, ""], [st, out], opts.inspect
      assert err.start_with?("tsr-report: "), "#{opts.inspect}: #{err}"
    end
    out, err, st = report
    assert_equal [2, ""], [st, out]
    assert err.start_with?("tsr-report: ")
    out, err, st = report(a, path("missing.tsr"))
    assert_equal [1, "", "tsr-report: #{path('missing.tsr')}: cannot read\n"], [st, out, err]
  end

  # R18: tsr-filter output reports like the selection; --latest reports like tsr merge.
  def test_tools_compose
    file = write("p.tsr", packed)
    [[], %w[--prefix al], %w[--tag red], %w[--since 5 --until 10], %w[--tag blue --until 9]].each do |opts|
      FileUtils.rm_f(path("f.tsr"))
      run_cmd(FILTER, *opts, file, path("f.tsr"))
      filtered, = report(path("f.tsr"))
      selected, = report(*opts, file)
      assert_equal lines(selected)[2..], lines(filtered)[2..], opts.inspect
    end
    a = write("a.tsr", Tessera.encode(FILE1))
    b = write("b.tsr", Tessera.encode(FILE2))
    [[a, b], [b, a], [file, a, b]].each do |files|
      run_cmd(TSR, "merge", "-o", path("m.tsr"), *files)
      [[], %w[--tag c], %w[--prefix user]].each do |opts|
        latest, = report("--latest", *opts, *files)
        merged, = report(*opts, path("m.tsr"))
        assert_equal lines(latest)[2..], lines(merged)[2..], "#{files.inspect} #{opts.inspect}"
      end
      lists = files.map { |f| Tessera.read(f) }
      assert_equal Tessera.encode(Tessera.merge(lists)), File.binread(path("m.tsr"))
    end
  end
end
