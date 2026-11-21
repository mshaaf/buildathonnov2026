# Local checks. The scorer builds ./Dockerfile; locally the sqlglot download stage
# can be swapped for a local checkout with SQLGLOT_CTX=/path/containing/sqlglot.
IMAGE ?= blast-radius-entry
KIT := $(CURDIR)/RWXblast-radius-sample
SAMPLE := $(KIT)/sample
OUTPUT := $(CURDIR)/output.jsonl
CTX_FLAG := $(if $(SQLGLOT_CTX),--build-context sqlglotsrc=$(SQLGLOT_CTX),)

RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build sample
build:
	docker build --platform linux/amd64 $(CTX_FLAG) --tag $(IMAGE) .

sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 $(KIT)/score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)
