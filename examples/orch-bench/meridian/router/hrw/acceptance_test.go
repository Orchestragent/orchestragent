package hrw

// Hidden acceptance: R3 and R8.

import (
	"reflect"
	"testing"
)

func TestAcceptanceScore(t *testing.T) {
	cases := []struct {
		node, key string
		want      uint64
	}{
		{"n1", "alpha", 16698026282089534036},
		{"n2", "alpha", 2484404034048581421},
		{"node-α", "ключ", 8599979163672630911},
		{"", "", 12638153115695167455},
		{"n3", "user:42", 15662658606299588827},
	}
	for _, c := range cases {
		if got := Score(c.node, c.key); got != c.want {
			t.Errorf("Score(%q, %q) = %d, want %d", c.node, c.key, got, c.want)
		}
	}
}

func TestAcceptanceRank(t *testing.T) {
	nodes := []string{"n1", "n2", "n3", "n4", "n5"}
	cases := map[string][]string{
		"alpha":   {"n1", "n5", "n3", "n2", "n4"},
		"beta":    {"n1", "n5", "n4", "n3", "n2"},
		"user:42": {"n3", "n4", "n2", "n1", "n5"},
		"ключ":    {"n1", "n5", "n4", "n2", "n3"},
		"a b%=c":  {"n1", "n5", "n3", "n4", "n2"},
		"":        {"n4", "n5", "n2", "n3", "n1"},
	}
	for key, want := range cases {
		input := append([]string(nil), nodes...)
		got := Rank(key, input)
		if !reflect.DeepEqual(got, want) {
			t.Errorf("Rank(%q) = %v, want %v", key, got, want)
		}
		if !reflect.DeepEqual(input, nodes) {
			t.Errorf("Rank(%q) modified its input: %v", key, input)
		}
	}
	if got := Rank("alpha", nil); len(got) != 0 {
		t.Errorf("Rank of no nodes = %v", got)
	}
	// equal scores (the same id twice) keep byte order and both entries
	if got := Rank("alpha", []string{"n2", "n2", "n1"}); !reflect.DeepEqual(got, []string{"n1", "n2", "n2"}) {
		t.Errorf("Rank with duplicates = %v", got)
	}
}
