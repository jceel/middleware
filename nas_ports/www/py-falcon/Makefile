# Created by: Zhihao Yuan <lichray@gmail.com>
# $FreeBSD$

PORTNAME=	falcon
PORTVERSION=	1.0.0
CATEGORIES=	www python
PKGNAMEPREFIX=	${PYTHON_PKGNAMEPREFIX}

MAINTAINER=	lichray@gmail.com
COMMENT=	High-performance Python framework for building cloud APIs

LICENSE=	APACHE20

BUILD_DEPENDS=	cython>=0.11.2:lang/cython
RUN_DEPENDS=	${PYTHON_PKGNAMEPREFIX}testtools>=0:devel/py-testtools \
		${PYTHON_PKGNAMEPREFIX}six>=1.1.0:devel/py-six \
		${PYTHON_PKGNAMEPREFIX}mimeparse>0:devel/py-mimeparse

USES=		python
USE_PYTHON=	distutils autoplist

USE_GITHUB=	yes
GH_ACCOUNT=	falconry
GH_TAGNAME=	1.0.0

.include <bsd.port.mk>
