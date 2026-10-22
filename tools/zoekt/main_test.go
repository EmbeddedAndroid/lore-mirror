package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestPathBoundary(t *testing.T) {
	for _, p := range []string{"foo", "foo/a", "foo/a\nb"} {
		if !allowedPath(p, []string{"foo"}) {
			t.Fatal(p)
		}
	}
	for _, p := range []string{"foobar", "x/foo"} {
		if allowedPath(p, []string{"foo"}) {
			t.Fatal(p)
		}
	}
}
func TestValidation(t *testing.T) {
	r := request{IndexDir: "/tmp/index", Commit: strings.Repeat("a", 40), Pattern: "hello", Mode: "literal", CaseSensitive: true, MaxMatches: 10, MaxBytes: 1024, TimeoutMS: 10}
	if err := validate(&r); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{"/etc/passwd", "a/../b", "a//b", "a/", "\x00"} {
		r.Paths = []string{p}
		if validate(&r) == nil {
			t.Fatal(p)
		}
	}
	r.Paths = []string{"name\nwith\ttabs", "--literal", ":literal"}
	if err := validate(&r); err != nil {
		t.Fatal(err)
	}
	r.Paths, r.Mode = nil, "regex"
	if validate(&r) == nil {
		t.Fatal("regex mode must be rejected")
	}
}
func TestMalformedRequestBounds(t *testing.T) {
	for _, input := range []string{`{}`, `{"unknown":1}`, `{} {}`, strings.Repeat("x", 65537)} {
		var out bytes.Buffer
		if run(strings.NewReader(input), &out) != 1 {
			t.Fatal("accepted invalid request")
		}
		if out.Len() > 1024 {
			t.Fatal("oversized error")
		}
		var res response
		if err := json.Unmarshal(out.Bytes(), &res); err != nil {
			t.Fatal(err)
		}
		if res.Error == "" || res.EngineCommit != engineCommit || len(res.Matches) != 0 {
			t.Fatal(res)
		}
	}
}
func TestLiteralNoQueryInjection(t *testing.T) {
	q, err := buildQuery(request{Pattern: `needle or repo:other`, Mode: "literal", CaseSensitive: true, Paths: []string{"a.b"}})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(q.String(), `needle or repo:other`) {
		t.Fatal(q.String())
	}
}

func TestIndependentWatchdog(t *testing.T) {
	fired := make(chan struct{})
	stop := watchdog(time.Millisecond, func() { close(fired) })
	defer stop()
	select {
	case <-fired:
	case <-time.After(time.Second):
		t.Fatal("watchdog did not expire independently")
	}
	canceled := make(chan struct{})
	stop = watchdog(20*time.Millisecond, func() { close(canceled) })
	stop()
	select {
	case <-canceled:
		t.Fatal("stopped watchdog fired")
	case <-time.After(40 * time.Millisecond):
	}
}
