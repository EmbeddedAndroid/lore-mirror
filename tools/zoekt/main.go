// lore-zoekt-query is a one-request, offline, bounded JSON adapter. It opens only
// a caller-validated immutable index generation. The caller enforces process
// memory, overall wall time (including shard loading), and stdout/stderr limits.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"regexp/syntax"
	"runtime"
	"runtime/debug"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/sourcegraph/zoekt"
	"github.com/sourcegraph/zoekt/query"
	"github.com/sourcegraph/zoekt/search"
)

const engineCommit = "e54cd91d1a64864fda7e681c9d31171eaab09ca7"
const engineVersion = "v0.0.0-20261020162159-e54cd91d1a64"
const requestLimit = 65536
const maxEngineMatches = 10000

type request struct {
	Op            string   `json:"op"`
	IndexDir      string   `json:"index_dir"`
	Commit        string   `json:"commit"`
	Pattern       string   `json:"pattern"`
	Mode          string   `json:"mode"`
	Paths         []string `json:"paths"`
	CaseSensitive bool     `json:"case_sensitive"`
	MaxMatches    int      `json:"max_matches"`
	MaxBytes      int      `json:"max_bytes"`
	TimeoutMS     int      `json:"timeout_ms"`
	MaxLineBytes  int      `json:"max_line_bytes"`
}
type match struct {
	Path          string `json:"path"`
	Line          int    `json:"line"`
	Text          string `json:"text"`
	Commit        string `json:"commit"`
	TextTruncated bool   `json:"text_truncated"`
}
type response struct {
	Repository     string   `json:"repository,omitempty"`
	CommitOID      string   `json:"commit_oid,omitempty"`
	Files          int      `json:"files,omitempty"`
	IndexFormat    int      `json:"index_format,omitempty"`
	HasSymbols     bool     `json:"has_symbols"`
	EngineCommit   string   `json:"engine_commit"`
	Commit         string   `json:"commit"`
	Matches        []match  `json:"matches"`
	Truncated      bool     `json:"truncated"`
	PartialReasons []string `json:"partial_reasons"`
	Error          string   `json:"error,omitempty"`
}

func (r *response) partial(reason string) {
	r.Truncated = true
	for _, old := range r.PartialReasons {
		if old == reason {
			return
		}
	}
	r.PartialReasons = append(r.PartialReasons, reason)
}
func validate(r *request) error {
	if !regexp.MustCompile(`^[0-9a-f]{40}$`).MatchString(r.Commit) {
		return errors.New("invalid_commit")
	}
	if !filepath.IsAbs(r.IndexDir) {
		return errors.New("index_dir_must_be_absolute")
	}
	if r.Op == "" {
		r.Op = "search"
	}
	if r.Op != "search" && r.Op != "verify" {
		return errors.New("invalid_op")
	}
	if r.Op == "verify" {
		if r.MaxBytes == 0 {
			r.MaxBytes = 1024
		}
		if r.TimeoutMS == 0 {
			r.TimeoutMS = 5000
		}
		if r.MaxMatches == 0 {
			r.MaxMatches = 1
		}
	} else if len(r.Pattern) == 0 || len(r.Pattern) > 8192 || !utf8.ValidString(r.Pattern) {
		return errors.New("invalid_pattern")
	}
	if r.Op != "verify" && r.Mode != "literal" && r.Mode != "symbol" {
		return errors.New("invalid_mode")
	}
	if r.MaxMatches < 1 || r.MaxMatches > maxEngineMatches || r.MaxBytes < 1024 || r.MaxBytes > 1048576 || r.TimeoutMS < 1 || r.TimeoutMS > 30000 {
		return errors.New("invalid_limits")
	}
	if r.MaxLineBytes == 0 {
		r.MaxLineBytes = 4096
	}
	if r.MaxLineBytes < 1 || r.MaxLineBytes > 8192 || len(r.Paths) > 128 {
		return errors.New("invalid_limits")
	}
	for _, p := range r.Paths {
		if len(p) == 0 || len(p) > 4096 || !utf8.ValidString(p) || strings.HasPrefix(p, "/") || strings.ContainsRune(p, 0) {
			return errors.New("invalid_path")
		}
		for _, part := range strings.Split(p, "/") {
			if part == "" || part == "." || part == ".." {
				return errors.New("invalid_path")
			}
		}
	}
	return nil
}
func buildQuery(r request) (query.Q, error) {
	var atom query.Q = &query.Substring{Pattern: r.Pattern, Content: true, CaseSensitive: r.CaseSensitive}
	if r.Mode == "symbol" {
		atom = &query.Symbol{Expr: atom}
	}
	if len(r.Paths) > 0 {
		parts := make([]string, 0, len(r.Paths))
		for _, p := range r.Paths {
			parts = append(parts, regexp.QuoteMeta(p))
		}
		re, err := syntax.Parse("^(?:"+strings.Join(parts, "|")+")(?:/|$)", syntax.Perl)
		if err != nil {
			return nil, err
		}
		atom = query.NewAnd(atom, &query.Regexp{Regexp: re, FileName: true, CaseSensitive: true})
	}
	return atom, nil
}
func allowedPath(name string, paths []string) bool {
	if len(paths) == 0 {
		return true
	}
	for _, p := range paths {
		if name == p || strings.HasPrefix(name, p+"/") {
			return true
		}
	}
	return false
}
func pinnedBuild() bool {
	b, ok := debug.ReadBuildInfo()
	if !ok {
		return false
	}
	for _, d := range b.Deps {
		if d.Path == "github.com/sourcegraph/zoekt" {
			return d.Version == engineVersion && d.Replace == nil
		}
	}
	return false
}
func execute(r request, out *response) error {
	if !pinnedBuild() {
		return errors.New("engine_build_pin_mismatch")
	}
	var q query.Q
	var err error
	if r.Op != "verify" {
		q, err = buildQuery(r)
		if err != nil {
			return err
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(r.TimeoutMS)*time.Millisecond)
	defer cancel()
	entries, err := os.ReadDir(r.IndexDir)
	if err != nil {
		return errors.New("index_open_failed")
	}
	shardCount := 0
	for _, entry := range entries {
		if strings.HasSuffix(entry.Name(), ".zoekt") {
			info, e := entry.Info()
			if e != nil || !info.Mode().IsRegular() {
				return errors.New("invalid_shard_file")
			}
			shardCount++
		}
	}
	if shardCount == 0 {
		return errors.New("index_metadata_invalid")
	}
	s, err := search.NewDirectorySearcher(r.IndexDir)
	if err != nil {
		return errors.New("index_open_failed")
	}
	defer s.Close()
	// Reject empty, mixed-repository, multi-ref, or wrong-commit generations even
	// for no-hit queries. The Python caller separately validates manifest hashes.
	repos, err := s.List(ctx, &query.Const{Value: true}, &zoekt.ListOptions{})
	if err != nil || repos.Crashes != 0 || len(repos.Repos) != 1 || repos.Stats.Shards != shardCount {
		return errors.New("index_metadata_invalid")
	}
	if r.Mode == "symbol" && !repos.Repos[0].Repository.HasSymbols {
		return errors.New("symbols_not_indexed")
	}
	branches := repos.Repos[0].Repository.Branches
	if len(branches) != 1 || branches[0].Version != r.Commit {
		return errors.New("index_commit_mismatch")
	}
	resRepo := repos.Repos[0]
	out.Repository = resRepo.Repository.Name
	out.CommitOID = r.Commit
	out.Files = resRepo.Stats.Documents
	out.IndexFormat = resRepo.IndexMetadata.IndexFormatVersion
	out.HasSymbols = resRepo.Repository.HasSymbols
	if r.Op == "verify" {
		return nil
	}
	q = query.NewAnd(q, &query.Branch{Pattern: branches[0].Name, Exact: true})
	// Zoekt counts occurrences, not lines, and fills its quota from the
	// highest-ranked files first. Capping it at the line limit returned fewer
	// lines than requested when one file repeats the pattern, so the engine
	// gets the bridge ceiling and the line loop below enforces MaxMatches.
	opts := &zoekt.SearchOptions{ShardMaxMatchCount: maxEngineMatches, TotalMaxMatchCount: maxEngineMatches, MaxDocDisplayCount: maxEngineMatches, MaxMatchDisplayCount: maxEngineMatches, MaxWallTime: time.Duration(r.TimeoutMS) * time.Millisecond, NumContextLines: 0}
	result, err := s.Search(ctx, q, opts)
	if err != nil {
		return errors.New("index_search_failed")
	}
	if result.Crashes != 0 {
		return errors.New("index_search_crashed")
	}
	if ctx.Err() != nil {
		out.partial("query_timeout")
	}
	if result.FilesSkipped > 0 || result.ShardsSkipped > 0 {
		out.partial("engine_search_limit")
	}
	if result.MatchCount >= maxEngineMatches {
		out.partial("match_limit")
	}
	for _, f := range result.Files {
		if f.Version != r.Commit {
			return errors.New("result_commit_mismatch")
		}
		if len(f.FileName) == 0 {
			return errors.New("invalid_result_path")
		}
		if len(f.FileName) > 4096 || !utf8.ValidString(f.FileName) {
			out.partial("path_limit")
			continue
		}
		if !allowedPath(f.FileName, r.Paths) {
			return errors.New("result_path_mismatch")
		}
		for _, l := range f.LineMatches {
			if l.FileName {
				continue
			}
			if len(out.Matches) >= r.MaxMatches {
				out.partial("match_limit")
				return nil
			}
			text := bytes.TrimSuffix(l.Line, []byte{'\n'})
			cut := false
			if len(text) > r.MaxLineBytes {
				text = text[:r.MaxLineBytes]
				cut = true
				out.partial("line_limit")
			}
			for !utf8.Valid(text) && len(text) > 0 && cut {
				text = text[:len(text)-1]
			}
			if !utf8.Valid(text) {
				out.partial("non_utf8_content")
				continue
			}
			out.Matches = append(out.Matches, match{f.FileName, l.LineNumber, string(text), r.Commit, cut})
		}
	}
	return nil
}

// The context does not bound initial shard loading. This independent watchdog
// also expires if the Python supervisor dies while the inherited I/O lock is held.
func watchdog(duration time.Duration, expire func()) func() {
	timer := time.AfterFunc(duration, expire)
	return func() { timer.Stop() }
}

func run(in io.Reader, out io.Writer) int {
	res := response{EngineCommit: engineCommit, Matches: []match{}, PartialReasons: []string{}}
	capBytes := 1024
	data, err := io.ReadAll(io.LimitReader(in, requestLimit+1))
	var r request
	if err == nil && len(data) > requestLimit {
		err = errors.New("request_too_large")
	}
	if err == nil {
		decoder := json.NewDecoder(bytes.NewReader(data))
		decoder.DisallowUnknownFields()
		if decoder.Decode(&r) != nil {
			err = errors.New("invalid_request")
		} else {
			var extra any
			if decoder.Decode(&extra) != io.EOF {
				err = errors.New("invalid_request")
			}
		}
	}
	if err == nil {
		err = validate(&r)
	}
	if err == nil {
		capBytes = r.MaxBytes
		stop := watchdog(time.Duration(r.TimeoutMS)*time.Millisecond, func() { os.Exit(124) })
		defer stop()
		res.Commit = r.Commit
		err = execute(r, &res)
	}
	exitCode := 0
	if err != nil {
		res.Error = err.Error()
		res.Matches = []match{}
		exitCode = 1
	}
	// Count encoded matches once rather than repeatedly marshalling shrinking
	// responses. Output clipping is linear and allocates only bounded chunks.
	all := res.Matches
	res.Matches = []match{}
	base, _ := json.Marshal(res)
	used, keep := len(base)+1, 0
	sizes := make([]int, 0, len(all))
	for _, item := range all {
		encodedItem, _ := json.Marshal(item)
		extra := len(encodedItem)
		if keep > 0 {
			extra++
		}
		if used+extra > capBytes {
			break
		}
		used += extra
		sizes = append(sizes, extra)
		keep++
	}
	if keep < len(all) {
		res.partial("output_limit")
		newBase, _ := json.Marshal(res)
		used += len(newBase) - len(base)
		for keep > 0 && used > capBytes {
			keep--
			used -= sizes[keep]
		}
	}
	res.Matches = all[:keep]
	encoded, _ := json.Marshal(res)
	if len(encoded)+1 > capBytes {
		res = response{EngineCommit: engineCommit, Matches: []match{}, PartialReasons: []string{}, Error: "output_limit"}
		encoded, _ = json.Marshal(res)
		exitCode = 1
	}
	if _, e := fmt.Fprintf(out, "%s\n", encoded); e != nil {
		return 1
	}
	return exitCode
}
func main() {
	stop := watchdog(35*time.Second, func() { os.Exit(124) })
	defer stop()
	runtime.GOMAXPROCS(1)
	if len(os.Args) == 2 && os.Args[1] == "--version" {
		if !pinnedBuild() {
			fmt.Fprintln(os.Stderr, "engine_build_pin_mismatch")
			os.Exit(1)
		}
		fmt.Printf("{\"engine_commit\":\"%s\",\"format_version\":1}\n", engineCommit)
		return
	}
	if len(os.Args) != 1 {
		fmt.Fprintln(os.Stderr, "unexpected_arguments")
		os.Exit(1)
	}
	os.Exit(run(os.Stdin, os.Stdout))
}
