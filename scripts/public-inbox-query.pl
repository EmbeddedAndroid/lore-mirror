#!/usr/bin/perl
# Read-only adapter for pinned public-inbox internals. No daemon and no networking.
# Input: one JSON object on stdin; argv[0] is the trusted extindex path.
# Output: bounded JSONL skeleton records, never full MIME bodies.
use v5.12;
use strict;
use warnings;
use JSON::PP;
use POSIX qw(strftime);
use PublicInbox::ExtSearch;
use PublicInbox::Address;

my $root = shift @ARGV;
@ARGV and die "unexpected arguments\n";
my $payload = '';
while (read(STDIN, my $chunk, 4096)) {
    $payload .= $chunk;
    length($payload) <= 65536 or die "request exceeds 65536 bytes\n";
}
my $req = decode_json($payload);
ref($req) eq 'HASH' or die "request must be an object\n";
$root //= $req->{extindex};
defined($root) && !ref($root) or die "extindex path required\n";
($req->{op} // 'search') eq 'search' or die "unsupported operation\n";
my $limit = $req->{limit} // 100;
my $offset = $req->{offset} // 0;
$limit =~ /\A\d+\z/ && $limit > 0 && $limit <= 10001 or die "limit out of bounds\n";
$offset =~ /\A\d+\z/ && $offset <= 1000000 or die "offset out of bounds\n";
my $query = $req->{query} // '';
$query = 'd:..' if $query =~ /\A\s*\z/ || $query eq '*';
length($query) <= 4096 && $query !~ /\x00/ or die "invalid query\n";
PublicInbox::Search::load_xapian();
my $search = PublicInbox::ExtSearch->new($root);
my $over = $search->over // die "external overview unavailable\n";
my $opt = { limit => 0 + $limit, offset => 0 + $offset, relevance => 0 };
if (defined $req->{thread_id}) {
    my $mid = $req->{thread_id};
    $mid =~ s/\A<|>\z//g;
    length($mid) <= 1000 or die "thread id too long\n";
    # This explicit no-result branch is required by upstream fix d6b9850.
    my $tid = $over->mid2tid($mid);
    exit(0) unless defined $tid;
    $opt->{threadid} = $tid;
}
$opt->{eidx_key} = $req->{eidx_key} if defined $req->{eidx_key};
$search->query_approxidate($search->git, $query);
if (exists $req->{eidx_keys}) {
    my $keys = $req->{eidx_keys};
    ref($keys) eq 'ARRAY' && @$keys <= 1000 or die "invalid scope keys\n";
    exit(0) unless @$keys;
    my $qp = $search->{qp} // $search->qparse_new;
    # O is public-inbox's physical external-inbox membership term, the same
    # term used by Search::mset's eidx_key filter. Apply before the result cap.
    $qp->add_boolean_prefix('local_archive_scope', 'O');
    $search->{qp} = $qp;
    my @terms;
    for my $key (@$keys) {
        defined($key) && !ref($key) && length($key) <= 2048 && $key !~ /\x00/
            or die "invalid scope key\n";
        $key =~ s/([\\"])/\\$1/g;
        push @terms, 'local_archive_scope:"'.$key.'"';
    }
    $query = '('.$query.') AND ('.join(' OR ', @terms).')';
}
my $mset = $search->mset($query, $opt);
my $ids = $search->mset_to_artnums($mset, $opt);
my $json = JSON::PP->new->utf8->canonical;
my $total_bytes = 0;
my %selected_keys = map { $_ => 1 } @{$req->{eidx_keys} // []};
for my $id (@$ids) {
    my $s = $over->get_art($id) // next;
    next unless $s->{mid} && $s->{blob};
    my @emails = PublicInbox::Address::emails($s->{from} // '');
    my $xrefs = $over->get_xref3($id);
    my %lists;
    my $scoped_blob;
    for my $xref (@$xrefs) {
        my ($group, $blob) = $xref =~ /\A(.*):\d+:([a-f0-9]+)\z/;
        next unless defined $group;
        $scoped_blob //= $blob if $selected_keys{$group};
        $group =~ s/\Aorg\.kernel\.lore\.//;
        $lists{$group} = 1;
    }
    my $row = {
        message_id => $s->{mid}, blob => ($scoped_blob // $s->{blob}), subject => $s->{subject} // '',
        from_email => $emails[0] // '', from => $s->{from} // '',
        date => defined($s->{ds}) ? strftime('%Y-%m-%dT%H:%M:%SZ', gmtime($s->{ds})) : undef,
        received_date => defined($s->{ts}) ? strftime('%Y-%m-%dT%H:%M:%SZ', gmtime($s->{ts})) : undef,
        list_ids => [sort keys %lists], references => $s->{references} // '',
        bytes => 0 + ($s->{bytes} // 0), docid => 0 + $id,
    };
    my $line = $json->encode($row) . "\n";
    $total_bytes += length($line);
    # Fail loudly rather than silently clipping. The parent already applies a
    # tighter process-output budget and returns explicit continuation metadata.
    $total_bytes <= 32 * 1024 * 1024 or die "query helper output budget exceeded\n";
    print $line;
}
