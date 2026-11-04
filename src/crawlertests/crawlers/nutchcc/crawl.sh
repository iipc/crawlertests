#!/bin/bash

# run a crawl using Common Crawl's fork of Apache Nutch and store content in WARC files
#
# This is crawl.sh from https://github.com/commoncrawl/cc-nutch-example with
# these changes for crawlertests, marked "crawlertests:" below:
#  - Nutch is in the image ($NUTCH_HOME) and the configuration in $CONF_DIR
#  - the fetch lists are larger: every case is on the same host, and the
#    example's 100 URLs per host per cycle would leave most of them unfetched
#  - the WARC metadata names the run, and the Nutch build is logged

# command-line arguments
CRAWL_DIR="$1"
NUM_CYCLES="$2"
SEED_DIR="$3"

# crawlertests: Nutch from the image, configuration from the harness
NUTCH_HOME="${NUTCH_HOME:-/opt/cc-nutch-example/nutch-cc/runtime/local}"
CONF_DIR="${CONF_DIR:-$(dirname $0)/conf}"
cat /opt/revisions.txt

# total size of fetch lists
TOPN=100000  # crawlertests: was 1000
# size of fetch lists per host
TOPN_PER_HOST=10000  # crawlertests: was 100
# max. time spent for fetching per cycle
MAX_FETCH_TIME_MINUTES=$((1+100/10))  # crawlertests: as for the example's 100 per host
# parallel threads used for fetching
FETCHER_THREADS=64


# WARC options (please modify)
WARC_OUTPUT_DIR="$CRAWL_DIR"/warc
WARC_OPERATOR="IIPC crawlertests run"  # crawlertests
DATE=$(date +%Y-%m-%d)
WARC_PUBLISHER="$WARC_OPERATOR"
WARC_PREFIX="CRAWLERTESTS"  # crawlertests
WARC_ISPARTOF="$WARC_PREFIX-$DATE"
WARC_DESCRIPTION="crawlertests, Apache Nutch, $DATE"  # crawlertests
WARC_SOFTWARE="Apache Nutch $(sed -n 's/^nutch-version //p' /opt/revisions.txt) (modified, https://github.com/commoncrawl/nutch/)"  # crawlertests
WARC_OPTIONS=(
    -Dfetcher.store.warc=true
    -Dwarc.deduplicate=true
    -Dwarc.export.text=false
    -Dwarc.export.crawldiagnostics=true
    -Dwarc.export.robotstxt=true
    -Dwarc.export.cdx=true
    -Dwarc.export.prefix="$WARC_PREFIX"
    -Dwarc.export.operator="$WARC_OPERATOR"
    -Dwarc.export.publisher="$WARC_PUBLISHER"
    -Dwarc.export.software="$WARC_SOFTWARE"
    -Dwarc.export.description="$WARC_DESCRIPTION"
    -Dwarc.export.isPartOf="$WARC_ISPARTOF"
    -Dwarc.detect.language=true
    -Dmapreduce.reduce.env=LD_PRELOAD=libcld2_full.so
)


function nutch() {
    nutch_tool="$1"; shift
    echo "nutch $nutch_tool $@"

    # configure log directory
    export NUTCH_LOG_DIR=$CRAWL_DIR/logs
    # make Nutch pick configuration files first from $CONF_DIR
    export NUTCH_CONF_DIR=$CONF_DIR:$NUTCH_HOME/conf

    $NUTCH_HOME/bin/nutch "$nutch_tool" "$@"

    RETCODE=$?
    if [ $RETCODE -eq 0 ]; then
        : # ok: no error
    elif [ "$nutch_tool" == "generate" ] && [ $RETCODE -eq 1 ]; then
        echo "Generate returned 1 (no segment created / no more URLs to fetch)"
        exit 0  # crawlertests: nothing left to crawl
    else
        echo "Error running:"
        echo "  $NUTCH_HOME/bin/nutch $nutch_tool $@"
        echo "Failed with exit value $RETCODE (there may be more details in ${NUTCH_LOG_DIR}/hadoop.log )"
        exit $RETCODE
    fi
}


if [ -n "$SEED_DIR" ] && ! [ -z "$SEED_DIR"  ]; then
    echo "Injecting seed URLs"
    nutch inject "${commonOptions[@]}" "$CRAWL_DIR"/crawldb "$SEED_DIR"
fi



for cycle in $(seq 1 $NUM_CYCLES); do
    echo "cycle $cycle"

    nutch generate \
          -Dgenerate.max.count=$TOPN_PER_HOST \
          -Dgenerate.count.mode=host \
          -Dgenerate.min.score=0.0 \
          "$CRAWL_DIR"/crawldb "$CRAWL_DIR"/segments \
          -topN $TOPN -noFilter -noNorm

    SEGMENT=$(ls "$CRAWL_DIR"/segments/ | sort -n | tail -n 1)

    nutch fetch \
          -Dfetcher.parse=true \
          -Dfetcher.timelimit.mins=$MAX_FETCH_TIME_MINUTES \
          "${WARC_OPTIONS[@]}" \
          -Dwarc.export.path=$WARC_OUTPUT_DIR/$SEGMENT \
          -Dwarc.export.cdx.path=$WARC_OUTPUT_DIR/$SEGMENT/cdx \
          "$CRAWL_DIR"/segments/$SEGMENT \
          -threads $FETCHER_THREADS
    # TODO: increase number of WARC files (num reducers)
    #       if size of fetch list (or top-N) exceeds 50k

    nutch updatedb "$CRAWL_DIR"/crawldb "$CRAWL_DIR"/segments/$SEGMENT

done
