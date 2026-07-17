guestbook api + frontend written in 3am

features:
- rate limits of 2 entries per day, and 3 likes per day
- low effort sql database so you can actually preserve the entries

you won't need docker to run it, but i recommend doing so

for shorthand i've already added restart, stop and start scripts to root directory.

just remember to set up the ports used in the docker-compose file before running the app, that's all

for live demo you can visit my [personal website](https://arda0.net/guestbook).
