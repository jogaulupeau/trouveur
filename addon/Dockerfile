ARG BUILD_FROM
FROM ${BUILD_FROM}

# Trouveur n'utilise que la bibliotheque standard : rien a installer.
WORKDIR /opt/trouveur
COPY app/ /opt/trouveur/
COPY apply_options.py /opt/trouveur/apply_options.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

# Les donnees vivent dans /data, volume persistant fourni par le superviseur :
# le contenu de /opt/trouveur est remplace a chaque mise a jour, pas celui-ci.
ENV TROUVEUR_DATA_DIR=/data

CMD [ "/run.sh" ]
