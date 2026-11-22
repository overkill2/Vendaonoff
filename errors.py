"""Hierarquia única de erros: quem chama só precisa conhecer estas classes."""


class AfiliadosError(Exception):
    """Base de todos os erros do sistema."""


class ErroConfiguracao(AfiliadosError):
    """Configuração ausente ou inválida."""


class ErroAutenticacao(AfiliadosError):
    """Token do webhook ausente ou incorreto."""


class ErroRequisicao(AfiliadosError):
    """Payload inválido (HTTP 400)."""


class ErroRota(ErroRequisicao):
    """Rota inexistente (HTTP 404)."""


class ErroBanco(AfiliadosError):
    """Falha no SQLite."""


class ErroDados(AfiliadosError):
    """Dado inconsistente ou inexistente (ex.: produto não cadastrado)."""


class ErroLLM(AfiliadosError):
    """Falha ao falar com o modelo de linguagem (após as retentativas do SDK)."""


class ErroSocial(AfiliadosError):
    """Base dos erros de integração com redes sociais."""


class ErroSocialTransitorio(ErroSocial):
    """Falha temporária (rede, limite de taxa, 5xx).

    seguro_repetir=True  -> a requisição certamente NÃO foi processada; repetir é seguro.
    seguro_repetir=False -> resultado ambíguo (timeout, 5xx); repetir uma publicação pode duplicá-la.
    """

    def __init__(self, mensagem: str, *, seguro_repetir: bool = True) -> None:
        super().__init__(mensagem)
        self.seguro_repetir = seguro_repetir


class ErroSocialIncerto(ErroSocial):
    """A publicação pode ter sido feita. Não repetimos sozinhos: exige verificação manual."""


class ErroSocialPermanente(ErroSocial):
    """Erro que não adianta repetir (token inválido, permissão, imagem recusada)."""
