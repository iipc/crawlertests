package org.netpreserve.crawlertests;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.apache.storm.Config;
import org.apache.storm.LocalCluster;
import org.apache.storm.generated.KillOptions;
import org.apache.storm.hdfs.bolt.rotation.FileSizeRotationPolicy;
import org.apache.storm.topology.TopologyBuilder;
import org.apache.storm.tuple.Fields;
import org.apache.storm.utils.Utils;
import org.apache.stormcrawler.Constants;
import org.apache.stormcrawler.bolt.FeedParserBolt;
import org.apache.stormcrawler.bolt.FetcherBolt;
import org.apache.stormcrawler.bolt.JSoupParserBolt;
import org.apache.stormcrawler.bolt.SiteMapParserBolt;
import org.apache.stormcrawler.bolt.URLPartitionerBolt;
import org.apache.stormcrawler.tika.ParserBolt;
import org.apache.stormcrawler.tika.RedirectionBolt;
import org.apache.stormcrawler.warc.WARCFileNameFormat;
import org.apache.stormcrawler.warc.WARCHdfsBolt;

/**
 * Runs the archetype's crawl topology (fetch, sitemap, feed, jsoup parse, Tika for other documents) in a local
 * cluster, with an in-memory frontier in place of URLFrontier and a WARC bolt on the fetcher's output. Stops once
 * the frontier is exhausted and every tuple has been acked.
 *
 * <p>Usage: CrawlTopology conf.json seeds.txt warc-dir
 */
public class CrawlTopology {

    public static void main(String[] args) throws Exception {
        Config conf = new Config();
        conf.putAll(configSection(Utils.findAndReadConfigFile("crawler-default.yaml", true)));
        conf.putAll(configSection(Utils.findAndReadConfigFile(args[0], true)));
        List<String> seeds = Files.readAllLines(Path.of(args[1])).stream().filter(s -> !s.isBlank()).toList();
        String warcDir = Path.of(args[2]).toAbsolutePath().toString();

        // the local file system without checksum files, as the WARC module's README recommends
        conf.put("warc", Map.of("fs.file.impl", "org.apache.hadoop.fs.RawLocalFileSystem"));

        Map<String, String> warcInfo = new LinkedHashMap<>();
        warcInfo.put("software", (String) conf.getOrDefault("crawlertests.software", "Apache StormCrawler"));
        warcInfo.put("format", "WARC File Format 1.1");
        warcInfo.put("conformsTo", "https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1/");
        WARCHdfsBolt warcBolt = new WARCHdfsBolt();
        WARCFileNameFormat fileNameFormat = new WARCFileNameFormat();
        fileNameFormat.withPath(warcDir);
        fileNameFormat.withPrefix("crawlertests");
        warcBolt.withFileNameFormat(fileNameFormat);
        warcBolt.withRotationPolicy(new FileSizeRotationPolicy(1, FileSizeRotationPolicy.Units.GB));
        warcBolt.withHeader(warcInfo);
        warcBolt.withRequestRecords();
        warcBolt.withConfigKey("warc");
        // sync (and so ack) every record, so the crawl's end isn't held up waiting for a sync
        warcBolt.withSyncPolicy(new org.apache.storm.hdfs.bolt.sync.CountSyncPolicy(1));

        TopologyBuilder builder = new TopologyBuilder();
        builder.setSpout("spout", new QueueSpout(seeds));
        builder.setBolt("partitioner", new URLPartitionerBolt()).shuffleGrouping("spout");
        builder.setBolt("fetch", new FetcherBolt()).fieldsGrouping("partitioner", new Fields("key"));
        builder.setBolt("warc", warcBolt).localOrShuffleGrouping("fetch");
        builder.setBolt("sitemap", new SiteMapParserBolt()).localOrShuffleGrouping("fetch");
        builder.setBolt("feed", new FeedParserBolt()).localOrShuffleGrouping("sitemap");
        builder.setBolt("parse", new JSoupParserBolt()).localOrShuffleGrouping("feed");
        builder.setBolt("shunt", new RedirectionBolt()).localOrShuffleGrouping("parse");
        builder.setBolt("tika", new ParserBolt()).localOrShuffleGrouping("shunt", "tika");
        Fields url = new Fields("url");
        builder.setBolt("status", new QueueStatusUpdater())
                .fieldsGrouping("fetch", Constants.StatusStreamName, url)
                .fieldsGrouping("sitemap", Constants.StatusStreamName, url)
                .fieldsGrouping("feed", Constants.StatusStreamName, url)
                .fieldsGrouping("parse", Constants.StatusStreamName, url)
                .fieldsGrouping("tika", Constants.StatusStreamName, url);

        try (LocalCluster cluster = new LocalCluster()) {
            cluster.submitTopology("crawlertests", conf, builder.createTopology());
            int idle = 0;
            while (idle < 3) {
                Thread.sleep(200);
                idle = QueueSpout.finished() ? idle + 1 : 0;
            }
            System.out.println("crawlertests: crawl finished after " + QueueSpout.emitted() + " URLs");
            KillOptions kill = new KillOptions();
            kill.set_wait_secs(0);
            cluster.killTopologyWithOpts("crawlertests", kill);
            // wait for the bolts to be cleaned up, which closes the WARC files
            while (!cluster.getTopologySummaries().isEmpty()) {
                Thread.sleep(200);
            }
        }
        System.exit(0);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> configSection(Map<String, Object> yaml) {
        Object section = yaml.get("config");
        return section instanceof Map ? (Map<String, Object>) section : yaml;
    }
}
