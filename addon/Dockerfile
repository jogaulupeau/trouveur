ARG BUILD_FROM
FROM ${BUILD_FROM}

# Trouveur n'utilise que la bibliotheque standard, a une exception pres :
# openssl, indispensable pour lire un certificat client au format PKCS#12
# (.p12/.pfx), que le module ssl de Python ne sait pas ouvrir.
RUN apk add --no-cache openssl

WORKDIR /opt/trouveur
COPY app/ /opt/trouveur/
COPY apply_options.py /opt/trouveur/apply_options.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

# Les donnees vivent dans /data, volume persistant fourni par le superviseur :
# le contenu de /opt/trouveur est remplace a chaque mise a jour, pas celui-ci.
ENV TROUVEUR_DATA_DIR=/data

CMD [ "/run.sh" ]
