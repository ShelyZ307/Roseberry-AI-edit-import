# Roseberry Supported JSON Schema

Preferred shape:

```text
object with top-level segments array
```

Required segment fields:

```text
start_time
end_time
```

Strongly recommended segment fields:

```text
index
title
duration_sec
hook
start_reason
end_reason
```

Supported compatibility top-level shapes:

```text
root array
object.moments
object.segments
object.data.moments
object.data.segments
```

Supported start aliases:

```text
start_time
start
in
in_time
start_time_seconds
```

Supported end aliases:

```text
end_time
end
out
out_time
end_time_seconds
```

Preferred time format:

```text
HH:MM:SS.mmm
```

Preferred time base:

```text
source-relative, starting at 00:00:00.000
```

Numeric seconds and `MM:SS` are supported compatibility formats.

`01:00:00` Resolve timeline-label timecodes can be normalized only for `Create Edited Timeline` when safe. For published JSON, use source-relative `00:00:00.000`.

Excel `.xlsx` compatibility is separate from the JSON schema.

