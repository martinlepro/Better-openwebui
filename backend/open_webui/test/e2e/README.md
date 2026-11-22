# Tests de bout en bout (vrai backend, faux « Claude »)

Ces scripts démarrent le **vrai** backend Open WebUI et un faux serveur compatible OpenAI qui
simule Claude (réponse 429 avec `retry-after`, streaming, appels d'outils). Ils vérifient :
limite → pause → reprise automatique, priorités, notifications (socket.io), reprise depuis un
chat interactif, agent local (fichiers, commandes, serveur en arrière-plan), approbations, sécurité.

```bash
# 1) dépendances : pip install -r backend/requirements-slim.txt   (+ httpx, python-socketio[asyncio_client])
# 2) faux Claude (port 9911)
python3 fake_claude.py &
# 3) backend de test (port 8099, base neuve dans /tmp/owui-data, dossier agent /tmp/agent-ws)
mkdir -p /tmp/owui-data /tmp/agent-ws && ./start.sh &     # adaptez le chemin du venv dans start.sh
# 4) scénarios
python3 e2e.py       # file d'attente + limites + agent local + sécurité   (56 contrôles)
echo hello > /tmp/agent-ws/hello.txt
python3 e2e2.py      # commandes shell, serveur de dev, chemin interactif   (24 contrôles)
```

Les chemins (`/tmp/...`, ports 8099/9911) sont en dur : ce sont des tests jetables, pas à lancer
contre une installation réelle.
