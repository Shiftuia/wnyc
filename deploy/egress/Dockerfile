# Same pinned Squid base image as discount-scrapper/egress-proxy; config baked in (no host bind mount).
FROM ubuntu/squid@sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029
COPY squid.conf /etc/squid/squid.conf
USER proxy
ENTRYPOINT ["/usr/sbin/squid"]
CMD ["-f", "/etc/squid/squid.conf", "-NYC"]
