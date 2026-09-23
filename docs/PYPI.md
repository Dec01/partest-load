# partest-load

Time-based load harness of the [partest](https://pypi.org/project/partest/) family: load
profiles measured in **seconds, not request counts**, saturation and degradation metrics, and
an HTML dashboard that is assembled entirely offline.

```bash
pip install 'partest-load[report]'
```

Python 3.10+. Without the `report` extra a run still executes and its results are still
saved — only the HTML dashboard is missing. The extra carries `numpy`, `pandas`, `plotly` and
`Jinja2`, which weigh more than everything else together.

## Duration, not a request count

A run of "10 000 requests" finishes sooner the slower the system is: a degrading service
receives less load per second and therefore looks calmer. A request count measures our
patience, not the system's endurance.

Duration fixes the experiment on the load side and leaves the system's behaviour observable.
Two runs of the same duration are comparable; two runs of the same request count are not.

| Profile | What it does | What it answers |
|---|---|---|
| `linear` | constant load | does the system hold the given level |
| `ramp` | gradual rise to a plateau, then a decline | where degradation starts |
| `stress` | aggressive rise until failure | saturation point and degradation slope |
| `batch` | a finite set of requests at one level | does the system get through the list |

Metrics: RPS, latency percentiles, success and error share, response sizes; for `stress` also
the saturation point and the degradation slope. Mean latency is deliberately not reported —
an average over a long-tailed distribution hides exactly what a load run is made for.

## One command

```bash
partest-load \
  --endpoints my/endpoints.yaml --endpoint orders_create \
  --profile my/profiles/ramp.yaml \
  --results-root out/load --save-raw \
  --report out/load/dashboard.html
```

`--dry-run` parses and prints the configuration without sending a single request.
`--report-only` rebuilds the dashboard from runs already on disk.

**Load is sent only on an explicit command** — no warm-up on import, no background requests.
A tool that can hit a stand by accident does not get installed.

## A run is identified by its operation

The identity of a run is the **operation**, written as `METHOD /path`
(`GET /v1/items/{id}`), and it is derived from the endpoint description — there is no
separate argument for it, so it cannot disagree with what was actually loaded.

```text
<root>/<operation key>/<profile>/<time>__<operation key>__<profile>__<hex>.json
<root>/_latest/<operation key>__<profile>.json      pointer to the latest run
```

The operation key is a filesystem-safe name (a readable remainder plus eight hex characters).
The conversion is one-way, so the operation itself is stored **inside the run file** in its
original form, path `{placeholders}` included: that is what lets a measurement be matched with
outside data about the same endpoint, without decoding a directory name.

`--endpoint` stays a **caption** — the name you gave the endpoint yourself. It is written into
the run and shown in the report, but identifies nothing: rename it and the run history stays
where it is.

## Your values stay yours

Endpoint descriptions and profiles are your files. The package ships **schemas and engines**;
concrete endpoints, stand addresses and credentials never enter it, not even as examples. All
paths are passed explicitly — nothing is discovered relative to the current directory.

## The dashboard is built offline

No CDN, no external fonts, no libraries fetched from the network: `plotly.js` is embedded into
the file, taken from the installed `plotly` package or from a file you point at with
`--plotly-js`. If there is nowhere to take it from, the dashboard is built with tables and
says on the page why there are no charts.

## Early days

First release. Not yet implemented, and not to be relied upon: the `load.profile` artifact
export (`artifacts.py` does not exist — the run result is read by the dashboard only), reuse
of `partest` for authentication, headers, the endpoint list and secret redaction, and stopping
a run on profile thresholds for `linear` and `ramp` (only `stress` decides to abort).

The run record format is `2.0`.

## Links

Source and issues: **https://github.com/Dec01/partest-load**

## License

MIT.
