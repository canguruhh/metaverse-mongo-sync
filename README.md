# Metaverse MongoDB Sync
Docker image to sync from Metaverse mvsd to MongoDB.

# Run
You can build the image or use the image form docker hub.
``` bash
docker run cangr/mvsd-mongo-sync
```

# Setup
To configure the sync service you need to set the environment variables:
- MONGO_HOST
- MONGO_PORT
- MONGO_DB
- MVSD_HOST
- MVSD_PORT
