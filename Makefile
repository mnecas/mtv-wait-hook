IMAGE_REGISTRY ?= quay.io
IMAGE_ORG ?= kubev2v
IMAGE_NAME ?= mtv-wait-hook
IMAGE_TAG ?= latest
IMAGE ?= $(IMAGE_REGISTRY)/$(IMAGE_ORG)/$(IMAGE_NAME):$(IMAGE_TAG)

CONTAINER_CMD ?= podman
NAMESPACE ?= default

.PHONY: build push deploy deploy-hook deploy-target

build:
	$(CONTAINER_CMD) build -t $(IMAGE) -f Containerfile .

push: build
	$(CONTAINER_CMD) push $(IMAGE)

deploy: deploy-hook deploy-target

deploy-hook:
	oc apply -f rbac.yml
	oc apply -f hook.yml

deploy-target:
	sed 's/$${NAMESPACE}/$(NAMESPACE)/g' configmap.yml | oc apply -f -
