from datetime import datetime, date

from sqlalchemy import inspect

from app_settings.app_settings import AppSettings


class TxModel:

    def __init__(self, **kwargs):
        pass

    def insert(self):
        AppSettings.db().add(self)
        AppSettings.db().commit()
        AppSettings.db().close()

    def update(self):
        AppSettings.db().merge(self)
        AppSettings.db().commit()
        AppSettings.db().close()

    def delete(self):
        AppSettings.db().delete(self)
        AppSettings.db().commit()
        AppSettings.db().close()

    @classmethod
    def get(cls, *args, **kwargs):
        """
        :param args:
        :param kwargs:
        :return TxModel:
        """
        if args:
            kwargs[inspect(cls).primary_key[0].name] = args[0]
        item = cls.query(**kwargs).first()
        AppSettings.db().close()
        return item

    @classmethod
    def query(cls, **kwargs):
        items = AppSettings.db().query(cls).filter_by(**kwargs)
        return items

    def __iter__(self):
        for c in inspect(self).mapper.column_attrs:
            value = getattr(self, c.key)
            if isinstance(value, (datetime, date)):
                value = value.strftime('%Y-%m-%dT%H:%M:%SZ')
            yield (c.key, value)

    def clone(self):
        return self.__class__(**dict(self))
