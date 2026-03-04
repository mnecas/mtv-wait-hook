FROM registry.access.redhat.com/ubi9/ubi-minimal:latest

RUN microdnf install -y python3 python3-pip && \
    microdnf clean all

COPY requirements.txt /requirements.txt
RUN pip3 install --no-cache-dir -r /requirements.txt

COPY wait_for_console.py /wait_for_console.py

ENTRYPOINT ["python3", "/wait_for_console.py"]
