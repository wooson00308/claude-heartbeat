## dream-{slug_short}
- slug: {slug}
- prompt: /dream
- interval: 3h
- timeout: 10m
- condition: dream-prep check-unprocessed --slug={slug}
- notify: all
