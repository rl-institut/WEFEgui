#!/usr/local/bin/python
echo yes | python manage.py collectstatic && \
python manage.py compilemessages
python manage.py makemigrations users projects dashboard wefe && \
python manage.py migrate && \
python manage.py loaddata 'fixtures/fixture.json' && \
echo 'Completed Setup Successfully!!'
