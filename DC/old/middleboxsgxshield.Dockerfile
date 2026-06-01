FROM gramineproject/gramine

WORKDIR /opt/middleboxsgxshield

COPY middleboxsgx /opt/middleboxsgxshield/middleboxsgx
COPY middleboxsgx.manifest.sgx /opt/middleboxsgxshield/middleboxsgx.manifest.sgx
COPY middleboxsgx.sig /opt/middleboxsgxshield/middleboxsgx.sig
COPY jwks.dat /opt/middleboxsgxshield/jwks.dat
COPY middleboxsgxshield.entrypoint.sh /usr/local/bin/middleboxsgxshield.entrypoint.sh

RUN chmod +x /usr/local/bin/middleboxsgxshield.entrypoint.sh
RUN printf '#!/usr/bin/env sh\nexec /usr/local/bin/middleboxsgxshield.entrypoint.sh "$@"\n' > /opt/middleboxsgxshield/operator \
	&& chmod +x /opt/middleboxsgxshield/operator
RUN mkdir -p /home/bonsai/Desktop/MasterThesis/DC/certs /certs

WORKDIR /opt/middleboxsgxshield
ENTRYPOINT ["/usr/local/bin/middleboxsgxshield.entrypoint.sh"]
