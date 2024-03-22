<p align="center"><img src="static/full_logo.svg"  alt="SupaArchive logo"/></p>

SupaArchive is a web-based multi-site image-archival tool, designed to help you store and manage your collection in an organized manner.

It features built-in support for multiple import sites, has machine-learning powered image search, tag search, description and title translation, and more. 

# ⚠️This is pre-alpha software and not ready for anything but development use.

### Current supported sources
| Source                            | Import | Userscript |
|-----------------------------------|--------|------------|
| [Gelbooru](https://gelbooru.com/) | ✅      | 🚫         |
| [Pixiv](https://pixiv.net/)       | ✅      | ✅          |
| Local files                       | 🚫     | 🚫         |

### Requirements
- Python 3.8 or higher
- 6GB of RAM _**or**_ VRAM for machine learning features (can be lower, but search intelligence will be significantly reduced)
- S3 bucket for storage
- Redis server for caching
- MongoDB server for metadata storage
- RabbitMQ server for task queueing
- Docker and docker-compose for deployment
- Qdrant for image search
- DeepL API key for translations

Most of this gets handled by the docker-compose file, but you will need to set up the S3 bucket yourself.
