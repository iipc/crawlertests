package org.netpreserve.crawlertests;

import java.util.ArrayDeque;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.apache.storm.spout.SpoutOutputCollector;
import org.apache.storm.task.TopologyContext;
import org.apache.storm.topology.OutputFieldsDeclarer;
import org.apache.storm.topology.base.BaseRichSpout;
import org.apache.storm.tuple.Fields;
import org.apache.storm.tuple.Values;
import org.apache.stormcrawler.Metadata;

/**
 * An in-memory frontier that knows when the crawl is over: each URL is emitted once, and the crawl is finished when
 * the queue is empty and every emitted tuple tree has been acked or failed. Outlinks reach the queue (through
 * {@link QueueStatusUpdater}) before the tuple that discovered them is acked, so this can't end early. Runs in local
 * mode only, where the topology shares the JVM with {@link CrawlTopology}.
 */
public class QueueSpout extends BaseRichSpout {

    private static final ArrayDeque<Object[]> queue = new ArrayDeque<>();
    private static final Set<String> seen = new HashSet<>();
    private static final Set<Object> pending = new HashSet<>();
    private static long emitted = 0;

    private final List<String> seeds;
    private SpoutOutputCollector collector;

    public QueueSpout(List<String> seeds) {
        this.seeds = seeds;
    }

    /** Queue a URL unless it has been queued before. */
    public static void add(String url, Metadata metadata) {
        synchronized (queue) {
            if (seen.add(url)) {
                queue.add(new Object[] {url, metadata});
            }
        }
    }

    public static boolean finished() {
        synchronized (queue) {
            return emitted > 0 && queue.isEmpty() && pending.isEmpty();
        }
    }

    public static long emitted() {
        synchronized (queue) {
            return emitted;
        }
    }

    @Override
    public void open(Map<String, Object> conf, TopologyContext context, SpoutOutputCollector collector) {
        this.collector = collector;
        for (String seed : seeds) {
            Metadata metadata = new Metadata();
            metadata.setValue("isSeed", "true");
            add(seed, metadata);
        }
    }

    @Override
    public void nextTuple() {
        synchronized (queue) {
            Object[] next = queue.poll();
            if (next == null) {
                return;
            }
            String url = (String) next[0];
            pending.add(url);
            emitted++;
            collector.emit(new Values(url, next[1]), url);
        }
    }

    @Override
    public void ack(Object msgId) {
        synchronized (queue) {
            pending.remove(msgId);
        }
    }

    @Override
    public void fail(Object msgId) {
        synchronized (queue) {
            pending.remove(msgId);
        }
    }

    @Override
    public void declareOutputFields(OutputFieldsDeclarer declarer) {
        declarer.declare(new Fields("url", "metadata"));
    }
}
