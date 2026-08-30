#!/usr/bin/with-contenv bashio
set -e

# /data est le volume persistant du superviseur : listes, cache et config y
# vivent, et survivent aux mises a jour de l'add-on.
export TROUVEUR_DATA_DIR=/data
mkdir -p /data

# Les options de l'interface Home Assistant sont reportees dans config.json,
# sans toucher aux cles qu'elles ne couvrent pas.
if ! python3 /opt/trouveur/apply_options.py; then
    bashio::log.fatal "Configuration incomplete : l'add-on ne peut pas demarrer."
    exit 1
fi

bashio::log.info "Trouveur demarre sur le port 8777 (ingress)."
exec python3 /opt/trouveur/server.py --no-browser
