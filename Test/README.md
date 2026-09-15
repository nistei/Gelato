# Gelato Tests

Python 3, standard library only. The tests require Docker.


## Running against a fresh instance

```shell

# Build the plugin
dotnet build Gelato.csproj -c Release

# Start a throwaway Jellyfin instance with the plugin installed
docker compose -f Test/docker-compose.tests.yml up -d

# Run the tests
# The addon URL is the AIOStreams manifest. Can also be set via ENV JF_ADDON_URL
python Test/e2e/run.py --destructive --addon-url <addon URL>

# throw the instance away
docker compose -f Test/docker-compose.tests.yml down -v
```

The addon URL is the AIOStreams manifest.
The setup creates the administrator `admin` with the password `jfapi` unless given otherwise.


## Running against an existing instance

The instance must run in a Docker container: the checks copy its database out with `docker cp`.
The destructive tests reconfigure the instance, so use a throwaway instance or a backup.

```shell
# Every non-destructive test, server on http://localhost:8096
python Test/e2e/run.py --container <name>                  
python Test/e2e/run.py --container <name> --url http://host:8096 --adminuser admin --adminpassword secret

# Some tests, verbose
python Test/e2e/run.py --container <name> play nextup -v

# Also the tests that reconfigure the instance and are destructive
python Test/e2e/run.py --container <name> --destructive

# Explicit items instead of automatic picks
python Test/e2e/run.py --container <name> play --movie <id> --row <id>
```
