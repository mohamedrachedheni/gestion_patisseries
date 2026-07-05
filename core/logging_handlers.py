from logging.handlers import TimedRotatingFileHandler


class SafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """TimedRotatingFileHandler tolérant au verrouillage de fichier Windows.

    Sous Windows, renommer un fichier encore ouvert par un autre processus
    lève PermissionError (WinError 32) — contrairement à POSIX. Si la
    rotation échoue pour cette raison, on continue simplement à écrire dans
    le fichier courant ; la rotation sera retentée à l'écriture suivante.
    Aucune ligne de log n'est perdue, seule la bascule de fichier est différée.
    """

    def doRollover(self):
        try:
            super().doRollover()
        except PermissionError:
            if self.stream is None:
                self.stream = self._open()
