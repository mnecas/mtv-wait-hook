IMAGE_REGISTRY ?= quay.io
IMAGE_ORG ?= kubev2v
IMAGE_NAME ?= mtv-wait-hook
IMAGE_TAG ?= latest
IMAGE ?= $(IMAGE_REGISTRY)/$(IMAGE_ORG)/$(IMAGE_NAME):$(IMAGE_TAG)

CONTAINER_CMD ?= podman
NAMESPACE ?= default

.PHONY: build push deploy deploy-hook deploy-target

all: build push deploy-hook deploy-target

build:
	$(CONTAINER_CMD) build -t $(IMAGE) -f post-hook/Containerfile post-hook

push: build
	$(CONTAINER_CMD) push $(IMAGE)

deploy: deploy-hook deploy-target

deploy-hook:
	oc apply -f post-hook/rbac.yml
	oc apply -f post-hook/hook.yml

deploy-target:
	sed 's/$${NAMESPACE}/$(NAMESPACE)/g' signal-script/configmap.yml | oc apply -f -
