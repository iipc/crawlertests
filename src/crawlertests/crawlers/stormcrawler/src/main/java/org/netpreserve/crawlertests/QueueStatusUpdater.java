package org.netpreserve.crawlertests;

import java.util.Date;
import java.util.Optional;
import org.apache.storm.tuple.Tuple;
import org.apache.stormcrawler.Metadata;
import org.apache.stormcrawler.persistence.AbstractStatusUpdaterBolt;
import org.apache.stormcrawler.persistence.Status;

/** Adds newly discovered URLs to the {@link QueueSpout}. Nothing is refetched. */
public class QueueStatusUpdater extends AbstractStatusUpdaterBolt {

    @Override
    public void store(String url, Status status, Metadata metadata, Optional<Date> nextFetch, Tuple t) {
        if (status == Status.DISCOVERED) {
            QueueSpout.add(url, metadata);
        }
        super.ack(t, url);
    }
}
