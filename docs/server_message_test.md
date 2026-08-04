# Beamline Server Message Test

Run these commands on the beamline server or on a machine that can reach:

```text
164.54.169.92
```

The scripts only print messages. They do not reduce data and do not write
analysis HDF5 files.

## ZMQ Test

Install dependency if needed:

```powershell
pip install pyzmq
```

Try the common Bluesky ZMQ ports:

```powershell
cd C:\Users\jiajtian\Documents\Playground\ASWAXS_reduction_pipeline_v6

python scripts\print_zmq_messages.py --address tcp://164.54.169.92:5578 --timeout-seconds 120
```

If that is quiet, try:

```powershell
python scripts\print_zmq_messages.py --address tcp://164.54.169.92:5577 --timeout-seconds 120
```

If the server uses a topic prefix:

```powershell
python scripts\print_zmq_messages.py --address tcp://164.54.169.92:5578 --topic asaxs --timeout-seconds 120
```

## Kafka Test

Install dependency if needed:

```powershell
pip install kafka-python
pip install bluesky-kafka
```

Try the current local default topic from the old publisher config:

```powershell
python scripts\print_kafka_messages.py `
  --bootstrap-servers 164.54.169.92:9092 `
  --topic asaxs.frames `
  --timeout-ms 120000
```

If the beamline uses a different topic, replace `asaxs.frames`.

## What To Send Back

Copy one or two printed messages and share them here. The most useful fields are:

```text
event/name
uid
scan_id
sample_name
detector
data_dir or filepath
topic/stream name
```

After we see the real payload shape, we can map it into the v3
`measurement_done` job schema.
