package ledger_test

import (
	"reflect"
	"strings"
	"testing"

	"example.com/ledger/ledger"
)

const header = "date,account,amount_cents,memo\n"

func TestAcceptanceR1ParsesRowsInOrder(t *testing.T) {
	got, err := ledger.ParseCSV(strings.NewReader(header +
		"2024-02-29,cash,-1250,\"coffee, beans\"\n" +
		"2024-03-01,bank,0,\n" +
		"2023-12-31,cash,99,\"said \"\"hi\"\"\"\n"))
	if err != nil {
		t.Fatal(err)
	}
	want := []ledger.Entry{
		{"2024-02-29", "cash", -1250, "coffee, beans"},
		{"2024-03-01", "bank", 0, ""},
		{"2023-12-31", "cash", 99, `said "hi"`},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
}

func TestAcceptanceR1RejectsWithLineNumbers(t *testing.T) {
	cases := map[string]string{
		"":                                 "line 1: ",
		"date,account,amount,memo\n":       "line 1: ",
		"account,date,amount_cents,memo\n": "line 1: ",
		header + "2024-02-30,cash,1,x\n":   "line 2: ",
		header + "2024-1-05,cash,1,x\n":    "line 2: ",
		header + "2024-01-05,cash,1,x\n2024-01-06, cash,1,x\n":  "line 3: ",
		header + "2024-01-05,,1,x\n":                            "line 2: ",
		header + "2024-01-05,cash,+5,x\n":                       "line 2: ",
		header + "2024-01-05,cash,007,x\n":                      "line 2: ",
		header + "2024-01-05,cash,1 ,x\n":                       "line 2: ",
		header + "2024-01-05,cash,1.5,x\n":                      "line 2: ",
		header + "2024-01-05,cash,99999999999999999999,x\n":     "line 2: ",
		header + "2024-01-05,cash,1\n":                          "line 2: ",
		header + "2024-01-05,cash,1,x\n2024-01-05,cash,1,x,y\n": "line 3: ",
	}
	for input, prefix := range cases {
		got, err := ledger.ParseCSV(strings.NewReader(input))
		if err == nil || !strings.HasPrefix(err.Error(), prefix) || got != nil {
			t.Errorf("%q: got %v, %v; want nil and an error starting %q", input, got, err, prefix)
		}
	}
}

func TestAcceptanceR2BalancesIncludeZeroSums(t *testing.T) {
	entries := []ledger.Entry{
		{"2024-01-01", "cash", 500, ""}, {"2024-01-02", "bank", 10, ""},
		{"2024-01-03", "cash", -500, ""},
	}
	before := append([]ledger.Entry(nil), entries...)
	got := ledger.Balances(entries)
	if !reflect.DeepEqual(got, map[string]int64{"cash": 0, "bank": 10}) {
		t.Fatalf("got %v", got)
	}
	if !reflect.DeepEqual(entries, before) {
		t.Fatal("input modified")
	}
	if len(ledger.Balances(nil)) != 0 {
		t.Fatal("no entries, no accounts")
	}
}

func TestAcceptanceR3ExactCanonicalBytes(t *testing.T) {
	got, err := ledger.ExportJSON([]ledger.Entry{
		{"2024-02-29", "zeta", -1250, `coffee & "beans" <hot>`},
		{"2024-03-01", "Alpha", 7, "café\n"},
		{"2024-03-02", "zeta", 1250, ""},
	})
	if err != nil {
		t.Fatal(err)
	}
	want := `{"balances":{"Alpha":7,"zeta":0},"entries":[` +
		`{"account":"zeta","amount_cents":-1250,"date":"2024-02-29","memo":"coffee & \"beans\" <hot>"},` +
		`{"account":"Alpha","amount_cents":7,"date":"2024-03-01","memo":"café\n"},` +
		`{"account":"zeta","amount_cents":1250,"date":"2024-03-02","memo":""}],"version":1}`
	if string(got) != want {
		t.Fatalf("got\n%s\nwant\n%s", got, want)
	}
	empty, err := ledger.ExportJSON(nil)
	if err != nil || string(empty) != `{"balances":{},"entries":[],"version":1}` {
		t.Fatalf("empty: %s %v", empty, err)
	}
}

func TestAcceptanceR3RejectsInvalidEntries(t *testing.T) {
	for _, bad := range []ledger.Entry{
		{"2024-13-01", "cash", 1, ""}, {"2024-01-01", " cash", 1, ""}, {"2024-01-01", "", 1, ""},
	} {
		out, err := ledger.ExportJSON([]ledger.Entry{{"2024-01-01", "ok", 1, ""}, bad})
		if err == nil || out != nil || !strings.HasPrefix(err.Error(), "entry 1") {
			t.Errorf("%v: got %s, %v", bad, out, err)
		}
	}
}
