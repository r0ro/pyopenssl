import datetime

from time import mktime
from base64 import b16encode
from functools import partial
from operator import __eq__, __ne__, __lt__, __le__, __gt__, __ge__
from warnings import warn as _warn

from six import (
    integer_types as _integer_types,
    text_type as _text_type,
    PY3 as _PY3)

from OpenSSL._util import (
    ffi as _ffi,
    lib as _lib,
    exception_from_error_queue as _exception_from_error_queue,
    byte_string as _byte_string,
    native as _native,
    UNSPECIFIED as _UNSPECIFIED,
    text_to_bytes_and_warn as _text_to_bytes_and_warn,
)

FILETYPE_PEM = _lib.SSL_FILETYPE_PEM
FILETYPE_ASN1 = _lib.SSL_FILETYPE_ASN1

# TODO This was an API mistake.  OpenSSL has no such constant.
FILETYPE_TEXT = 2 ** 16 - 1

TYPE_RSA = _lib.EVP_PKEY_RSA
TYPE_DSA = _lib.EVP_PKEY_DSA
if _lib.Cryptography_HAS_EC:
    TYPE_EC = _lib.EVP_PKEY_EC
else:
    TYPE_EC = None

class Error(Exception):
    """
    An error occurred in an `OpenSSL.crypto` API.
    """


_raise_current_error = partial(_exception_from_error_queue, Error)


def _untested_error(where):
    """
    An OpenSSL API failed somehow.  Additionally, the failure which was
    encountered isn't one that's exercised by the test suite so future behavior
    of pyOpenSSL is now somewhat less predictable.
    """
    raise RuntimeError("Unknown %s failure" % (where,))


def _new_mem_buf(buffer=None):
    """
    Allocate a new OpenSSL memory BIO.

    Arrange for the garbage collector to clean it up automatically.

    :param buffer: None or some bytes to use to put into the BIO so that they
        can be read out.
    """
    if buffer is None:
        bio = _lib.BIO_new(_lib.BIO_s_mem())
        free = _lib.BIO_free
    else:
        data = _ffi.new("char[]", buffer)
        bio = _lib.BIO_new_mem_buf(data, len(buffer))

        # Keep the memory alive as long as the bio is alive!
        def free(bio, ref=data):
            return _lib.BIO_free(bio)

    if bio == _ffi.NULL:
        # TODO: This is untested.
        _raise_current_error()

    bio = _ffi.gc(bio, free)
    return bio


def _bio_to_string(bio):
    """
    Copy the contents of an OpenSSL BIO object into a Python byte string.
    """
    result_buffer = _ffi.new('char**')
    buffer_length = _lib.BIO_get_mem_data(bio, result_buffer)
    return _ffi.buffer(result_buffer[0], buffer_length)[:]


def _set_asn1_time(boundary, when):
    """
    The the time value of an ASN1 time object.

    @param boundary: An ASN1_GENERALIZEDTIME pointer (or an object safely
        castable to that type) which will have its value set.
    @param when: A string representation of the desired time value.

    @raise TypeError: If C{when} is not a L{bytes} string.
    @raise ValueError: If C{when} does not represent a time in the required
        format.
    @raise RuntimeError: If the time value cannot be set for some other
        (unspecified) reason.
    """
    if not isinstance(when, bytes):
        raise TypeError("when must be a byte string")

    set_result = _lib.ASN1_GENERALIZEDTIME_set_string(
        _ffi.cast('ASN1_GENERALIZEDTIME*', boundary), when)
    if set_result == 0:
        dummy = _ffi.gc(_lib.ASN1_STRING_new(), _lib.ASN1_STRING_free)
        _lib.ASN1_STRING_set(dummy, when, len(when))
        check_result = _lib.ASN1_GENERALIZEDTIME_check(
            _ffi.cast('ASN1_GENERALIZEDTIME*', dummy))
        if not check_result:
            raise ValueError("Invalid string")
        else:
            _untested_error()


def _get_asn1_time(timestamp):
    """
    Retrieve the time value of an ASN1 time object.

    @param timestamp: An ASN1_GENERALIZEDTIME* (or an object safely castable to
        that type) from which the time value will be retrieved.

    @return: The time value from C{timestamp} as a L{bytes} string in a certain
        format.  Or C{None} if the object contains no time value.
    """
    string_timestamp = _ffi.cast('ASN1_STRING*', timestamp)
    if _lib.ASN1_STRING_length(string_timestamp) == 0:
        return None
    elif (
        _lib.ASN1_STRING_type(string_timestamp) == _lib.V_ASN1_GENERALIZEDTIME
    ):
        return _ffi.string(_lib.ASN1_STRING_data(string_timestamp))
    else:
        generalized_timestamp = _ffi.new("ASN1_GENERALIZEDTIME**")
        _lib.ASN1_TIME_to_generalizedtime(timestamp, generalized_timestamp)
        if generalized_timestamp[0] == _ffi.NULL:
            # This may happen:
            #   - if timestamp was not an ASN1_TIME
            #   - if allocating memory for the ASN1_GENERALIZEDTIME failed
            #   - if a copy of the time data from timestamp cannot be made for
            #     the newly allocated ASN1_GENERALIZEDTIME
            #
            # These are difficult to test.  cffi enforces the ASN1_TIME type.
            # Memory allocation failures are a pain to trigger
            # deterministically.
            _untested_error("ASN1_TIME_to_generalizedtime")
        else:
            string_timestamp = _ffi.cast(
                "ASN1_STRING*", generalized_timestamp[0])
            string_data = _lib.ASN1_STRING_data(string_timestamp)
            string_result = _ffi.string(string_data)
            _lib.ASN1_GENERALIZEDTIME_free(generalized_timestamp[0])
            return string_result


class PKey(object):
    """
    A class representing an DSA or RSA public key or key pair.
    """
    _only_public = False
    _initialized = True

    def __init__(self):
        pkey = _lib.EVP_PKEY_new()
        self._pkey = _ffi.gc(pkey, _lib.EVP_PKEY_free)
        self._initialized = False

    def generate_key(self, type, bits):
        """
        .. deprecated:: 0.15.2

            Use :func:`generate_rsa_key` or :func:`generate_dsa_key` instead.

        Generate a key pair of the given type, with the given number of bits.

        This generates a key "into" the this object.

        :param type: The key type.
        :type type: :data:`TYPE_RSA` or :data:`TYPE_DSA`
        :param bits: The number of bits.
        :type bits: :class:`int` ``>= 0``
        :raises TypeError: If type or bits isn't of the appropriate type.
        :raises ValueError: If the number of bits isn't an integer of
            the appropriate size.
        :return: :const:`None`
        """

        _warn("OpenSSL.crypto.Pkey.generate_key() is deprecated as of 15.2.0.",
              category=DeprecationWarning,
              stacklevel=2,
        )

        if not isinstance(type, int):
            raise TypeError("type must be an integer")

        if type == TYPE_RSA:
            self.generate_rsa_key(bits)
        elif type == TYPE_DSA:
            self.generate_dsa_key(bits)
        else:
            raise TypeError("No such key type")

    def generate_rsa_key(self, bits):
        """
        Generate a rsa key pair with the given number of bits.

        This generates a key "into" the this object.

        :param bits: The number of bits.
        :type bits: :class:`int` ``>= 0``
        :raises TypeError: If bits isn't of the appropriate type.
        :raises ValueError: If the number of bits isn't an integer of
            the appropriate size.
        :return: :const:`None`
        """

        if not isinstance(bits, int):
            raise TypeError("bits must be an integer")

        # TODO Check error return
        exponent = _lib.BN_new()
        exponent = _ffi.gc(exponent, _lib.BN_free)
        _lib.BN_set_word(exponent, _lib.RSA_F4)

        if bits <= 0:
            raise ValueError("Invalid number of bits")

        rsa = _lib.RSA_new()

        result = _lib.RSA_generate_key_ex(rsa, bits, exponent, _ffi.NULL)
        if result == 0:
            # TODO: The test for this case is commented out.  Different
            # builds of OpenSSL appear to have different failure modes that
            # make it hard to test.  Visual inspection of the OpenSSL
            # source reveals that a return value of 0 signals an error.
            # Manual testing on a particular build of OpenSSL suggests that
            # this is probably the appropriate way to handle those errors.
            _raise_current_error()

        result = _lib.EVP_PKEY_assign_RSA(self._pkey, rsa)
        if not result:
            # TODO: It appears as though this can fail if an engine is in
            # use which does not support RSA.
            _raise_current_error()

        self._initialized = True

    def generate_dsa_key(self, bits):
        """
        Generate a dsa key pair with the given number of bits.

        This generates a key "into" the this object.

        :param bits: The number of bits. If bits is < 512 DSA_generate_parameters
            will silently promote any value below 512 to 512.
        :type bits: :class:`int`
        :raises TypeError: If bits isn't of the appropriate type.
        :raises ValueError: If the number of bits isn't an integer of
            the appropriate size.
        :return: :const:`None`
        """

        if not isinstance(bits, int):
            raise TypeError("bits must be an integer")

        dsa = _lib.DSA_generate_parameters(
            bits, _ffi.NULL, 0, _ffi.NULL, _ffi.NULL, _ffi.NULL, _ffi.NULL)
        if dsa == _ffi.NULL:
            # TODO: This is untested.
            _raise_current_error()
        if not _lib.DSA_generate_key(dsa):
            # TODO: This is untested.
            _raise_current_error()
        if not _lib.EVP_PKEY_assign_DSA(self._pkey, dsa):
            # TODO: This is untested.
            _raise_current_error()

        self._initialized = True

    def generate_ec_key(self, curve_name):
        """
        Generate an ec key with the given curve name.

        This generated a key "into" the this object.

        :param curve_name: The name of the elliptic curve to use.
        :type curve_name: :class:`str`
        :raises TypeError: If curve_name is not a string.
        :raises ValueError: If curve_name is not a supported
            curve name.
        """
        if not isinstance(curve_name, str):
            raise TypeError("curve_name must be a string")

        curve = get_elliptic_curve(curve_name)
        ec = curve._to_EC_KEY()

        if not _lib.EC_KEY_generate_key(ec):
            # TODO: This is untested.
            _raise_current_error()
        if not _lib.EVP_PKEY_set1_EC_KEY(self._pkey, ec):
            # TODO: This is untested.
            _raise_current_error()

        self._initialized = True

    def check(self):
        """
        Check the consistency of an RSA private key.

        This is the Python equivalent of OpenSSL's ``RSA_check_key``.

        :return: True if key is consistent.
        :raise Error: if the key is inconsistent.
        :raise TypeError: if the key is of a type which cannot be checked.
            Only RSA keys can currently be checked.
        """
        if self._only_public:
            raise TypeError("public key only")

        if _lib.EVP_PKEY_type(self._pkey.type) != _lib.EVP_PKEY_RSA:
            raise TypeError("key type unsupported")

        rsa = _lib.EVP_PKEY_get1_RSA(self._pkey)
        rsa = _ffi.gc(rsa, _lib.RSA_free)
        result = _lib.RSA_check_key(rsa)
        if result:
            return True
        _raise_current_error()

    def type(self):
        """
        Returns the type of the key

        :return: The type of the key.
        """
        return self._pkey.type

    def bits(self):
        """
        Returns the number of bits of the key

        :return: The number of bits of the key.
        :raise TypeError: if the key is initialized and is of a type
            which does not have bits parameter.
        """

        if not self._initialized:
            return 0

        key_type = _lib.EVP_PKEY_type(self._pkey.type)
        if key_type != _lib.EVP_PKEY_RSA and key_type != _lib.EVP_PKEY_DSA:
            raise TypeError("key type unsupported")

        return _lib.EVP_PKEY_bits(self._pkey)
PKeyType = PKey


class _EllipticCurve(object):
    """
    A representation of a supported elliptic curve.

    @cvar _curves: :py:obj:`None` until an attempt is made to load the curves.
        Thereafter, a :py:type:`set` containing :py:type:`_EllipticCurve`
        instances each of which represents one curve supported by the system.
    @type _curves: :py:type:`NoneType` or :py:type:`set`
    """
    _curves = None

    if _PY3:
        # This only necessary on Python 3.  Morever, it is broken on Python 2.
        def __ne__(self, other):
            """
            Implement cooperation with the right-hand side argument of ``!=``.

            Python 3 seems to have dropped this cooperation in this very narrow
            circumstance.
            """
            if isinstance(other, _EllipticCurve):
                return super(_EllipticCurve, self).__ne__(other)
            return NotImplemented

    @classmethod
    def _load_elliptic_curves(cls, lib):
        """
        Get the curves supported by OpenSSL.

        :param lib: The OpenSSL library binding object.

        :return: A :py:type:`set` of ``cls`` instances giving the names of the
            elliptic curves the underlying library supports.
        """
        if lib.Cryptography_HAS_EC:
            num_curves = lib.EC_get_builtin_curves(_ffi.NULL, 0)
            builtin_curves = _ffi.new('EC_builtin_curve[]', num_curves)
            # The return value on this call should be num_curves again.  We
            # could check it to make sure but if it *isn't* then.. what could
            # we do? Abort the whole process, I suppose...?  -exarkun
            lib.EC_get_builtin_curves(builtin_curves, num_curves)
            return set(
                cls.from_nid(lib, c.nid)
                for c in builtin_curves)
        return set()

    @classmethod
    def _get_elliptic_curves(cls, lib):
        """
        Get, cache, and return the curves supported by OpenSSL.

        :param lib: The OpenSSL library binding object.

        :return: A :py:type:`set` of ``cls`` instances giving the names of the
            elliptic curves the underlying library supports.
        """
        if cls._curves is None:
            cls._curves = cls._load_elliptic_curves(lib)
        return cls._curves

    @classmethod
    def from_nid(cls, lib, nid):
        """
        Instantiate a new :py:class:`_EllipticCurve` associated with the given
        OpenSSL NID.

        :param lib: The OpenSSL library binding object.

        :param nid: The OpenSSL NID the resulting curve object will represent.
            This must be a curve NID (and not, for example, a hash NID) or
            subsequent operations will fail in unpredictable ways.
        :type nid: :py:class:`int`

        :return: The curve object.
        """
        return cls(lib, nid, _ffi.string(lib.OBJ_nid2sn(nid)).decode("ascii"))

    def __init__(self, lib, nid, name):
        """
        :param _lib: The :py:mod:`cryptography` binding instance used to
            interface with OpenSSL.

        :param _nid: The OpenSSL NID identifying the curve this object
            represents.
        :type _nid: :py:class:`int`

        :param name: The OpenSSL short name identifying the curve this object
            represents.
        :type name: :py:class:`unicode`
        """
        self._lib = lib
        self._nid = nid
        self.name = name

    def __repr__(self):
        return "<Curve %r>" % (self.name,)

    def _to_EC_KEY(self):
        """
        Create a new OpenSSL EC_KEY structure initialized to use this curve.

        The structure is automatically garbage collected when the Python object
        is garbage collected.
        """
        key = self._lib.EC_KEY_new_by_curve_name(self._nid)
        return _ffi.gc(key, _lib.EC_KEY_free)


def get_elliptic_curves():
    """
    Return a set of objects representing the elliptic curves supported in the
    OpenSSL build in use.

    The curve objects have a :py:class:`unicode` ``name`` attribute by which
    they identify themselves.

    The curve objects are useful as values for the argument accepted by
    :py:meth:`Context.set_tmp_ecdh` to specify which elliptical curve should be
    used for ECDHE key exchange.
    """
    return _EllipticCurve._get_elliptic_curves(_lib)


def get_elliptic_curve(name):
    """
    Return a single curve object selected by name.

    See :py:func:`get_elliptic_curves` for information about curve objects.

    :param name: The OpenSSL short name identifying the curve object to
        retrieve.
    :type name: :py:class:`unicode`

    If the named curve is not supported then :py:class:`ValueError` is raised.
    """
    for curve in get_elliptic_curves():
        if curve.name == name:
            return curve
    raise ValueError("unknown curve name", name)


class X509Name(object):
    """
    An X.509 Distinguished Name.

    :ivar countryName: The country of the entity.
    :ivar C: Alias for  :py:attr:`countryName`.

    :ivar stateOrProvinceName: The state or province of the entity.
    :ivar ST: Alias for :py:attr:`stateOrProvinceName`.

    :ivar localityName: The locality of the entity.
    :ivar L: Alias for :py:attr:`localityName`.

    :ivar organizationName: The organization name of the entity.
    :ivar O: Alias for :py:attr:`organizationName`.

    :ivar organizationalUnitName: The organizational unit of the entity.
    :ivar OU: Alias for :py:attr:`organizationalUnitName`

    :ivar commonName: The common name of the entity.
    :ivar CN: Alias for :py:attr:`commonName`.

    :ivar emailAddress: The e-mail address of the entity.
    """

    def __init__(self, name):
        """
        Create a new X509Name, copying the given X509Name instance.

        :param name: The name to copy.
        :type name: :py:class:`X509Name`
        """
        name = _lib.X509_NAME_dup(name._name)
        self._name = _ffi.gc(name, _lib.X509_NAME_free)

    def __setattr__(self, name, value):
        if name.startswith('_'):
            return super(X509Name, self).__setattr__(name, value)

        # Note: we really do not want str subclasses here, so we do not use
        # isinstance.
        if type(name) is not str:
            raise TypeError("attribute name must be string, not '%.200s'" % (
                type(value).__name__,))

        nid = _lib.OBJ_txt2nid(_byte_string(name))
        if nid == _lib.NID_undef:
            try:
                _raise_current_error()
            except Error:
                pass
            raise AttributeError("No such attribute")

        # If there's an old entry for this NID, remove it
        for i in range(_lib.X509_NAME_entry_count(self._name)):
            ent = _lib.X509_NAME_get_entry(self._name, i)
            ent_obj = _lib.X509_NAME_ENTRY_get_object(ent)
            ent_nid = _lib.OBJ_obj2nid(ent_obj)
            if nid == ent_nid:
                ent = _lib.X509_NAME_delete_entry(self._name, i)
                _lib.X509_NAME_ENTRY_free(ent)
                break

        if isinstance(value, _text_type):
            value = value.encode('utf-8')

        add_result = _lib.X509_NAME_add_entry_by_NID(
            self._name, nid, _lib.MBSTRING_UTF8, value, -1, -1, 0)
        if not add_result:
            _raise_current_error()

    def __getattr__(self, name):
        """
        Find attribute. An X509Name object has the following attributes:
        countryName (alias C), stateOrProvince (alias ST), locality (alias L),
        organization (alias O), organizationalUnit (alias OU), commonName
        (alias CN) and more...
        """
        nid = _lib.OBJ_txt2nid(_byte_string(name))
        if nid == _lib.NID_undef:
            # This is a bit weird.  OBJ_txt2nid indicated failure, but it seems
            # a lower level function, a2d_ASN1_OBJECT, also feels the need to
            # push something onto the error queue.  If we don't clean that up
            # now, someone else will bump into it later and be quite confused.
            # See lp#314814.
            try:
                _raise_current_error()
            except Error:
                pass
            return super(X509Name, self).__getattr__(name)

        entry_index = _lib.X509_NAME_get_index_by_NID(self._name, nid, -1)
        if entry_index == -1:
            return None

        entry = _lib.X509_NAME_get_entry(self._name, entry_index)
        data = _lib.X509_NAME_ENTRY_get_data(entry)

        result_buffer = _ffi.new("unsigned char**")
        data_length = _lib.ASN1_STRING_to_UTF8(result_buffer, data)
        if data_length < 0:
            # TODO: This is untested.
            _raise_current_error()

        try:
            result = _ffi.buffer(
                result_buffer[0], data_length
            )[:].decode('utf-8')
        finally:
            # XXX untested
            _lib.OPENSSL_free(result_buffer[0])
        return result

    def _cmp(op):
        def f(self, other):
            if not isinstance(other, X509Name):
                return NotImplemented
            result = _lib.X509_NAME_cmp(self._name, other._name)
            return op(result, 0)
        return f

    __eq__ = _cmp(__eq__)
    __ne__ = _cmp(__ne__)

    __lt__ = _cmp(__lt__)
    __le__ = _cmp(__le__)

    __gt__ = _cmp(__gt__)
    __ge__ = _cmp(__ge__)

    def __repr__(self):
        """
        String representation of an X509Name
        """
        result_buffer = _ffi.new("char[]", 512)
        format_result = _lib.X509_NAME_oneline(
            self._name, result_buffer, len(result_buffer))

        if format_result == _ffi.NULL:
            # TODO: This is untested.
            _raise_current_error()

        return "<X509Name object '%s'>" % (
            _native(_ffi.string(result_buffer)),)

    def hash(self):
        """
        Return an integer representation of the first four bytes of the
        MD5 digest of the DER representation of the name.

        This is the Python equivalent of OpenSSL's ``X509_NAME_hash``.

        :return: The (integer) hash of this name.
        :rtype: :py:class:`int`
        """
        return _lib.X509_NAME_hash(self._name)

    def der(self):
        """
        Return the DER encoding of this name.

        :return: The DER encoded form of this name.
        :rtype: :py:class:`bytes`
        """
        result_buffer = _ffi.new('unsigned char**')
        encode_result = _lib.i2d_X509_NAME(self._name, result_buffer)
        if encode_result < 0:
            # TODO: This is untested.
            _raise_current_error()

        string_result = _ffi.buffer(result_buffer[0], encode_result)[:]
        _lib.OPENSSL_free(result_buffer[0])
        return string_result

    def get_components(self):
        """
        Returns the components of this name, as a sequence of 2-tuples.

        :return: The components of this name.
        :rtype: :py:class:`list` of ``name, value`` tuples.
        """
        result = []
        for i in range(_lib.X509_NAME_entry_count(self._name)):
            ent = _lib.X509_NAME_get_entry(self._name, i)

            fname = _lib.X509_NAME_ENTRY_get_object(ent)
            fval = _lib.X509_NAME_ENTRY_get_data(ent)

            nid = _lib.OBJ_obj2nid(fname)
            name = _lib.OBJ_nid2sn(nid)

            result.append((
                _ffi.string(name),
                _ffi.string(
                    _lib.ASN1_STRING_data(fval),
                    _lib.ASN1_STRING_length(fval))))

        return result


X509NameType = X509Name


class X509Extension(object):
    """
    An X.509 v3 certificate extension.
    """

    def __init__(self, type_name, critical, value, subject=None, issuer=None):
        """
        Initializes an X509 extension.

        :param type_name: The name of the type of extension to create. See
            http://openssl.org/docs/apps/x509v3_config.html#STANDARD_EXTENSIONS
        :type type_name: :py:data:`bytes`

        :param bool critical: A flag indicating whether this is a critical
            extension.

        :param value: The value of the extension.
        :type value: :py:data:`bytes`

        :param subject: Optional X509 certificate to use as subject.
        :type subject: :py:class:`X509`

        :param issuer: Optional X509 certificate to use as issuer.
        :type issuer: :py:class:`X509`
        """
        ctx = _ffi.new("X509V3_CTX*")

        # A context is necessary for any extension which uses the r2i
        # conversion method.  That is, X509V3_EXT_nconf may segfault if passed
        # a NULL ctx. Start off by initializing most of the fields to NULL.
        _lib.X509V3_set_ctx(ctx, _ffi.NULL, _ffi.NULL, _ffi.NULL, _ffi.NULL, 0)

        # We have no configuration database - but perhaps we should (some
        # extensions may require it).
        _lib.X509V3_set_ctx_nodb(ctx)

        # Initialize the subject and issuer, if appropriate.  ctx is a local,
        # and as far as I can tell none of the X509V3_* APIs invoked here steal
        # any references, so no need to mess with reference counts or
        # duplicates.
        if issuer is not None:
            if not isinstance(issuer, X509):
                raise TypeError("issuer must be an X509 instance")
            ctx.issuer_cert = issuer._x509
        if subject is not None:
            if not isinstance(subject, X509):
                raise TypeError("subject must be an X509 instance")
            ctx.subject_cert = subject._x509

        if critical:
            # There are other OpenSSL APIs which would let us pass in critical
            # separately, but they're harder to use, and since value is already
            # a pile of crappy junk smuggling a ton of utterly important
            # structured data, what's the point of trying to avoid nasty stuff
            # with strings? (However, X509V3_EXT_i2d in particular seems like
            # it would be a better API to invoke.  I do not know where to get
            # the ext_struc it desires for its last parameter, though.)
            value = b"critical," + value

        extension = _lib.X509V3_EXT_nconf(_ffi.NULL, ctx, type_name, value)
        if extension == _ffi.NULL:
            _raise_current_error()
        self._extension = _ffi.gc(extension, _lib.X509_EXTENSION_free)

    @property
    def _nid(self):
        return _lib.OBJ_obj2nid(self._extension.object)

    _prefixes = {
        _lib.GEN_EMAIL: "email",
        _lib.GEN_DNS: "DNS",
        _lib.GEN_URI: "URI",
    }

    def _subjectAltNameString(self):
        method = _lib.X509V3_EXT_get(self._extension)
        if method == _ffi.NULL:
            # TODO: This is untested.
            _raise_current_error()
        payload = self._extension.value.data
        length = self._extension.value.length

        payloadptr = _ffi.new("unsigned char**")
        payloadptr[0] = payload

        if method.it != _ffi.NULL:
            ptr = _lib.ASN1_ITEM_ptr(method.it)
            data = _lib.ASN1_item_d2i(_ffi.NULL, payloadptr, length, ptr)
            names = _ffi.cast("GENERAL_NAMES*", data)
        else:
            names = _ffi.cast(
                "GENERAL_NAMES*",
                method.d2i(_ffi.NULL, payloadptr, length))

        names = _ffi.gc(names, _lib.GENERAL_NAMES_free)
        parts = []
        for i in range(_lib.sk_GENERAL_NAME_num(names)):
            name = _lib.sk_GENERAL_NAME_value(names, i)
            try:
                label = self._prefixes[name.type]
            except KeyError:
                bio = _new_mem_buf()
                _lib.GENERAL_NAME_print(bio, name)
                parts.append(_native(_bio_to_string(bio)))
            else:
                value = _native(
                    _ffi.buffer(name.d.ia5.data, name.d.ia5.length)[:])
                parts.append(label + ":" + value)
        return ", ".join(parts)

    def __str__(self):
        """
        :return: a nice text representation of the extension
        """
        if _lib.NID_subject_alt_name == self._nid:
            return self._subjectAltNameString()

        bio = _new_mem_buf()
        print_result = _lib.X509V3_EXT_print(bio, self._extension, 0, 0)
        if not print_result:
            # TODO: This is untested.
            _raise_current_error()

        return _native(_bio_to_string(bio))

    def get_critical(self):
        """
        Returns the critical field of this X.509 extension.

        :return: The critical field.
        """
        return _lib.X509_EXTENSION_get_critical(self._extension)

    def get_short_name(self):
        """
        Returns the short type name of this X.509 extension.

        The result is a byte string such as :py:const:`b"basicConstraints"`.

        :return: The short type name.
        :rtype: :py:data:`bytes`

        .. versionadded:: 0.12
        """
        obj = _lib.X509_EXTENSION_get_object(self._extension)
        nid = _lib.OBJ_obj2nid(obj)
        return _ffi.string(_lib.OBJ_nid2sn(nid))

    def get_data(self):
        """
        Returns the data of the X509 extension, encoded as ASN.1.

        :return: The ASN.1 encoded data of this X509 extension.
        :rtype: :py:data:`bytes`

        .. versionadded:: 0.12
        """
        octet_result = _lib.X509_EXTENSION_get_data(self._extension)
        string_result = _ffi.cast('ASN1_STRING*', octet_result)
        char_result = _lib.ASN1_STRING_data(string_result)
        result_length = _lib.ASN1_STRING_length(string_result)
        return _ffi.buffer(char_result, result_length)[:]


X509ExtensionType = X509Extension


class X509Req(object):
    """
    An X.509 certificate signing requests.
    """

    def __init__(self):
        req = _lib.X509_REQ_new()
        self._req = _ffi.gc(req, _lib.X509_REQ_free)

    def set_pubkey(self, pkey):
        """
        Set the public key of the certificate signing request.

        :param pkey: The public key to use.
        :type pkey: :py:class:`PKey`

        :return: :py:const:`None`
        """
        set_result = _lib.X509_REQ_set_pubkey(self._req, pkey._pkey)
        if not set_result:
            # TODO: This is untested.
            _raise_current_error()

    def get_pubkey(self):
        """
        Get the public key of the certificate signing request.

        :return: The public key.
        :rtype: :py:class:`PKey`
        """
        pkey = PKey.__new__(PKey)
        pkey._pkey = _lib.X509_REQ_get_pubkey(self._req)
        if pkey._pkey == _ffi.NULL:
            # TODO: This is untested.
            _raise_current_error()
        pkey._pkey = _ffi.gc(pkey._pkey, _lib.EVP_PKEY_free)
        pkey._only_public = True
        return pkey

    def set_version(self, version):
        """
        Set the version subfield (RFC 2459, section 4.1.2.1) of the certificate
        request.

        :param int version: The version number.
        :return: :py:const:`None`
        """
        set_result = _lib.X509_REQ_set_version(self._req, version)
        if not set_result:
            _raise_current_error()

    def get_version(self):
        """
        Get the version subfield (RFC 2459, section 4.1.2.1) of the certificate
        request.

        :return: The value of the version subfield.
        :rtype: :py:class:`int`
        """
        return _lib.X509_REQ_get_version(self._req)

    def get_subject(self):
        """
        Return the subject of this certificate signing request.

        This creates a new :py:class:`X509Name`: modifying it does not affect
        this request.

        :return: The subject of this certificate signing request.
        :rtype: :py:class:`X509Name`
        """
        name = X509Name.__new__(X509Name)
        name._name = _lib.X509_REQ_get_subject_name(self._req)
        if name._name == _ffi.NULL:
            # TODO: This is untested.
            _raise_current_error()

        # The name is owned by the X509Req structure.  As long as the X509Name
        # Python object is alive, keep the X509Req Python object alive.
        name._owner = self

        return name

    def add_extensions(self, extensions):
        """
        Add extensions to the certificate signing request.

        :param extensions: The X.509 extensions to add.
        :type extensions: iterable of :py:class:`X509Extension`
        :return: :py:const:`None`
        """
        stack = _lib.sk_X509_EXTENSION_new_null()
        if stack == _ffi.NULL:
            # TODO: This is untested.
            _raise_current_error()

        stack = _ffi.gc(stack, _lib.sk_X509_EXTENSION_free)

        for ext in extensions:
            if not isinstance(ext, X509Extension):
                raise ValueError("One of the elements is not an X509Extension")

            # TODO push can fail (here and elsewhere)
            _lib.sk_X509_EXTENSION_push(stack, ext._extension)

        add_result = _lib.X509_REQ_add_extensions(self._req, stack)
        if not add_result:
            # TODO: This is untested.
            _raise_current_error()

    def get_extensions(self):
        """
        Get X.509 extensions in the certificate signing request.

        :return: The X.509 extensions in this request.
        :rtype: :py:class:`list` of :py:class:`X509Extension` objects.

        .. versionadded:: 0.15
        """
        exts = []
        native_exts_obj = _lib.X509_REQ_get_extensions(self._req)
        for i in range(_lib.sk_X509_EXTENSION_num(native_exts_obj)):
            ext = X509Extension.__new__(X509Extension)
            ext._extension = _lib.sk_X509_EXTENSION_value(native_exts_obj, i)
            exts.append(ext)
        return exts

    def sign(self, pkey, digest):
        """
        Sign the certificate signing request with this key and digest type.

        :param pkey: The key pair to sign with.
        :type pkey: :py:class:`PKey`
        :param digest: The name of the message digest to use for the signature,
            e.g. :py:data:`b"sha1"`.
        :type digest: :py:class:`bytes`
        :return: :py:const:`None`
        """
        if pkey._only_public:
            raise ValueError("Key has only public part")

        if not pkey._initialized:
            raise ValueError("Key is uninitialized")

        digest_obj = _lib.EVP_get_digestbyname(_byte_string(digest))
        if digest_obj == _ffi.NULL:
            raise ValueError("No such digest method")

        sign_result = _lib.X509_REQ_sign(self._req, pkey._pkey, digest_obj)
        if not sign_result:
            # TODO: This is untested.
            _raise_current_error()

    def verify(self, pkey):
        """
        Verifies the signature on this certificate signing request.

        :param key: A public key.
        :type key: :py:class:`PKey`
        :return: :py:data:`True` if the signature is correct.
        :rtype: :py:class:`bool`
        :raises Error: If the signature is invalid or there is a
            problem verifying the signature.
        """
        if not isinstance(pkey, PKey):
            raise TypeError("pkey must be a PKey instance")

        result = _lib.X509_REQ_verify(self._req, pkey._pkey)
        if result <= 0:
            _raise_current_error()

        return result


X509ReqType = X509Req


class X509(object):
    """
    An X.509 certificate.
    """

    def __init__(self):
        # TODO Allocation failure?  And why not __new__ instead of __init__?
        x509 = _lib.X509_new()
        self._x509 = _ffi.gc(x509, _lib.X509_free)

    def set_version(self, version):
        """
        Set the version number of the certificate.

        :param version: The version number of the certificate.
        :type version: :py:class:`int`

        :return: :py:const:`None`
        """
        if not isinstance(version, int):
            raise TypeError("version must be an integer")

        _lib.X509_set_version(self._x509, version)

    def get_version(self):
        """
        Return the version number of the certificate.

        :return: The version number of the certificate.
        :rtype: :py:class:`int`
        """
        return _lib.X509_get_version(self._x509)

    def get_pubkey(self):
        """
        Get the public key of the certificate.

        :return: The public key.
        :rtype: :py:class:`PKey`
        """
        pkey = PKey.__new__(PKey)
        pkey._pkey = _lib.X509_get_pubkey(self._x509)
        if pkey._pkey == _ffi.NULL:
            _raise_current_error()
        pkey._pkey = _ffi.gc(pkey._pkey, _lib.EVP_PKEY_free)
        pkey._only_public = True
        return pkey

    def set_pubkey(self, pkey):
        """
        Set the public key of the certificate.

        :param pkey: The public key.
        :type pkey: :py:class:`PKey`

        :return: :py:data:`None`
        """
        if not isinstance(pkey, PKey):
            raise TypeError("pkey must be a PKey instance")

        set_result = _lib.X509_set_pubkey(self._x509, pkey._pkey)
        if not set_result:
            _raise_current_error()

    def sign(self, pkey, digest):
        """
        Sign the certificate with this key and digest type.

        :param pkey: The key to sign with.
        :type pkey: :py:class:`PKey`

        :param digest: The name of the message digest to use.
        :type digest: :py:class:`bytes`

        :return: :py:data:`None`
        """
        if not isinstance(pkey, PKey):
            raise TypeError("pkey must be a PKey instance")

        if pkey._only_public:
            raise ValueError("Key only has public part")

        if not pkey._initialized:
            raise ValueError("Key is uninitialized")

        evp_md = _lib.EVP_get_digestbyname(_byte_string(digest))
        if evp_md == _ffi.NULL:
            raise ValueError("No such digest method")

        sign_result = _lib.X509_sign(self._x509, pkey._pkey, evp_md)
        if not sign_result:
            _raise_current_error()

    def get_signature_algorithm(self):
        """
        Return the signature algorithm used in the certificate.

        :return: The name of the algorithm.
        :rtype: :py:class:`bytes`

        :raises ValueError: If the signature algorithm is undefined.

        .. versionadded:: 0.13
        """
        alg = self._x509.cert_info.signature.algorithm
        nid = _lib.OBJ_obj2nid(alg)
        if nid == _lib.NID_undef:
            raise ValueError("Undefined signature algorithm")
        return _ffi.string(_lib.OBJ_nid2ln(nid))

    def digest(self, digest_name):
        """
        Return the digest of the X509 object.

        :param digest_name: The name of the digest algorithm to use.
        :type digest_name: :py:class:`bytes`

        :return: The digest of the object, formatted as
            :py:const:`b":"`-delimited hex pairs.
        :rtype: :py:class:`bytes`
        """
        digest = _lib.EVP_get_digestbyname(_byte_string(digest_name))
        if digest == _ffi.NULL:
            raise ValueError("No such digest method")

        result_buffer = _ffi.new("char[]", _lib.EVP_MAX_MD_SIZE)
        result_length = _ffi.new("unsigned int[]", 1)
        result_length[0] = len(result_buffer)

        digest_result = _lib.X509_digest(
            self._x509, digest, result_buffer, result_length)

        if not digest_result:
            # TODO: This is untested.
            _raise_current_error()

        return b":".join([
            b16encode(ch).upper() for ch
            in _ffi.buffer(result_buffer, result_length[0])])

    def subject_name_hash(self):
        """
        Return the hash of the X509 subject.

        :return: The hash of the subject.
        :rtype: :py:class:`bytes`
        """
        return _lib.X509_subject_name_hash(self._x509)

    def set_serial_number(self, serial):
        """
        Set the serial number of the certificate.

        :param serial: The new serial number.
        :type serial: :py:class:`int`

        :return: :py:data`None`
        """
        if not isinstance(serial, _integer_types):
            raise TypeError("serial must be an integer")

        hex_serial = hex(serial)[2:]
        if not isinstance(hex_serial, bytes):
            hex_serial = hex_serial.encode('ascii')

        bignum_serial = _ffi.new("BIGNUM**")

        # BN_hex2bn stores the result in &bignum.  Unless it doesn't feel like
        # it.  If bignum is still NULL after this call, then the return value
        # is actually the result.  I hope.  -exarkun
        small_serial = _lib.BN_hex2bn(bignum_serial, hex_serial)

        if bignum_serial[0] == _ffi.NULL:
            set_result = _lib.ASN1_INTEGER_set(
                _lib.X509_get_serialNumber(self._x509), small_serial)
            if set_result:
                # TODO Not tested
                _raise_current_error()
        else:
            asn1_serial = _lib.BN_to_ASN1_INTEGER(bignum_serial[0], _ffi.NULL)
            _lib.BN_free(bignum_serial[0])
            if asn1_serial == _ffi.NULL:
                # TODO Not tested
                _raise_current_error()
            asn1_serial = _ffi.gc(asn1_serial, _lib.ASN1_INTEGER_free)
            set_result = _lib.X509_set_serialNumber(self._x509, asn1_serial)
            if not set_result:
                # TODO Not tested
                _raise_current_error()

    def get_serial_number(self):
        """
        Return the serial number of this certificate.

        :return: The serial number.
        :rtype: :py:class:`int`
        """
        asn1_serial = _lib.X509_get_serialNumber(self._x509)
        bignum_serial = _lib.ASN1_INTEGER_to_BN(asn1_serial, _ffi.NULL)
        try:
            hex_serial = _lib.BN_bn2hex(bignum_serial)
            try:
                hexstring_serial = _ffi.string(hex_serial)
                serial = int(hexstring_serial, 16)
                return serial
            finally:
                _lib.OPENSSL_free(hex_serial)
        finally:
            _lib.BN_free(bignum_serial)

    def gmtime_adj_notAfter(self, amount):
        """
        Adjust the time stamp on which the certificate stops being valid.

        :param amount: The number of seconds by which to adjust the timestamp.
        :type amount: :py:class:`int`

        :return: :py:const:`None`
        """
        if not isinstance(amount, int):
            raise TypeError("amount must be an integer")

        notAfter = _lib.X509_get_notAfter(self._x509)
        _lib.X509_gmtime_adj(notAfter, amount)

    def gmtime_adj_notBefore(self, amount):
        """
        Adjust the timestamp on which the certificate starts being valid.

        :param amount: The number of seconds by which to adjust the timestamp.
        :return: :py:const:`None`
        """
        if not isinstance(amount, int):
            raise TypeError("amount must be an integer")

        notBefore = _lib.X509_get_notBefore(self._x509)
        _lib.X509_gmtime_adj(notBefore, amount)

    def has_expired(self):
        """
        Check whether the certificate has expired.

        :return: :py:const:`True` if the certificate has expired,
            :py:const:`False` otherwise.
        :rtype: :py:class:`bool`
        """
        time_string = _native(self.get_notAfter())
        timestamp = mktime(datetime.datetime.strptime(
            time_string, "%Y%m%d%H%M%SZ").timetuple())
        now = mktime(datetime.datetime.utcnow().timetuple())

        return timestamp < now

    def _get_boundary_time(self, which):
        return _get_asn1_time(which(self._x509))

    def get_notBefore(self):
        """
        Get the timestamp at which the certificate starts being valid.

        The timestamp is formatted as an ASN.1 GENERALIZEDTIME::

            YYYYMMDDhhmmssZ
            YYYYMMDDhhmmss+hhmm
            YYYYMMDDhhmmss-hhmm

        :return: A timestamp string, or :py:const:`None` if there is none.
        :rtype: :py:class:`bytes` or :py:const:`None`
        """
        return self._get_boundary_time(_lib.X509_get_notBefore)

    def _set_boundary_time(self, which, when):
        return _set_asn1_time(which(self._x509), when)

    def set_notBefore(self, when):
        """
        Set the timestamp at which the certificate starts being valid.

        The timestamp is formatted as an ASN.1 GENERALIZEDTIME::

            YYYYMMDDhhmmssZ
            YYYYMMDDhhmmss+hhmm
            YYYYMMDDhhmmss-hhmm

        :param when: A timestamp string.
        :type when: :py:class:`bytes`

        :return: :py:const:`None`
        """
        return self._set_boundary_time(_lib.X509_get_notBefore, when)

    def get_notAfter(self):
        """
        Get the timestamp at which the certificate stops being valid.

        The timestamp is formatted as an ASN.1 GENERALIZEDTIME::

            YYYYMMDDhhmmssZ
            YYYYMMDDhhmmss+hhmm
            YYYYMMDDhhmmss-hhmm

        :return: A timestamp string, or :py:const:`None` if there is none.
        :rtype: :py:class:`bytes` or :py:const:`None`
        """
        return self._get_boundary_time(_lib.X509_get_notAfter)

    def set_notAfter(self, when):
        """
        Set the timestamp at which the certificate stops being valid.

        The timestamp is formatted as an ASN.1 GENERALIZEDTIME::

            YYYYMMDDhhmmssZ
            YYYYMMDDhhmmss+hhmm
            YYYYMMDDhhmmss-hhmm

        :param when: A timestamp string.
        :type when: :py:class:`bytes`

        :return: :py:const:`None`
        """
        return self._set_boundary_time(_lib.X509_get_notAfter, when)

    def _get_name(self, which):
        name = X509Name.__new__(X509Name)
        name._name = which(self._x509)
        if name._name == _ffi.NULL:
            # TODO: This is untested.
            _raise_current_error()

        # The name is owned by the X509 structure.  As long as the X509Name
        # Python object is alive, keep the X509 Python object alive.
        name._owner = self

        return name

    def _set_name(self, which, name):
        if not isinstance(name, X509Name):
            raise TypeError("name must be an X509Name")
        set_result = which(self._x509, name._name)
        if not set_result:
            # TODO: This is untested.
            _raise_current_error()

    def get_issuer(self):
        """
        Return the issuer of this certificate.

        This creates a new :py:class:`X509Name`: modifying it does not affect
        this certificate.

        :return: The issuer of this certificate.
        :rtype: :py:class:`X509Name`
        """
        return self._get_name(_lib.X509_get_issuer_name)

    def set_issuer(self, issuer):
        """
        Set the issuer of this certificate.

        :param issuer: The issuer.
        :type issuer: :py:class:`X509Name`

        :return: :py:const:`None`
        """
        return self._set_name(_lib.X509_set_issuer_name, issuer)

    def get_subject(self):
        """
        Return the subject of this certificate.

        This creates a new :py:class:`X509Name`: modifying it does not affect
        this certificate.

        :return: The subject of this certificate.
        :rtype: :py:class:`X509Name`
        """
        return self._get_name(_lib.X509_get_subject_name)

    def set_subject(self, subject):
        """
        Set the subject of this certificate.

        :param subject: The subject.
        :type subject: :py:class:`X509Name`

        :return: :py:const:`None`
        """
        return self._set_name(_lib.X509_set_subject_name, subject)

    def get_extension_count(self):
        """
        Get the number of extensions on this certificate.

        :return: The number of extensions.
        :rtype: :py:class:`int`

        .. versionadded:: 0.12
        """
        return _lib.X509_get_ext_count(self._x509)

    def add_extensions(self, extensions):
        """
        Add extensions to the certificate.

        :param extensions: The extensions to add.
        :type extensions: An iterable of :py:class:`X509Extension` objects.
        :return: :py:const:`None`
        """
        for ext in extensions:
            if not isinstance(ext, X509Extension):
                raise ValueError("One of the elements is not an X509Extension")

            add_result = _lib.X509_add_ext(self._x509, ext._extension, -1)
            if not add_result:
                _raise_current_error()

    def get_extension(self, index):
        """
        Get a specific extension of the certificate by index.

        Extensions on a certificate are kept in order. The index
        parameter selects which extension will be returned.

        :param int index: The index of the extension to retrieve.
        :return: The extension at the specified index.
        :rtype: :py:class:`X509Extension`
        :raises IndexError: If the extension index was out of bounds.

        .. versionadded:: 0.12
        """
        ext = X509Extension.__new__(X509Extension)
        ext._extension = _lib.X509_get_ext(self._x509, index)
        if ext._extension == _ffi.NULL:
            raise IndexError("extension index out of bounds")

        extension = _lib.X509_EXTENSION_dup(ext._extension)
        ext._extension = _ffi.gc(extension, _lib.X509_EXTENSION_free)
        return ext


X509Type = X509


class X509Store(object):
    """
    An X509 certificate store.
    """

    def __init__(self):
        store = _lib.X509_STORE_new()
        self._store = _ffi.gc(store, _lib.X509_STORE_free)

    def add_cert(self, cert):
        """
        Adds the certificate :py:data:`cert` to this store.

        This is the Python equivalent of OpenSSL's ``X509_STORE_add_cert``.

        :param X509 cert: The certificate to add to this store.
        :raises TypeError: If the certificate is not an :py:class:`X509`.
        :raises Error: If OpenSSL was unhappy with your certificate.
        :return: :py:data:`None` if the certificate was added successfully.
        """
        if not isinstance(cert, X509):
            raise TypeError()

        result = _lib.X509_STORE_add_cert(self._store, cert._x509)
        if not result:
            _raise_current_error()


X509StoreType = X509Store


class X509StoreContextError(Exception):
    """
    An exception raised when an error occurred while verifying a certificate
    using `OpenSSL.X509StoreContext.verify_certificate`.

    :ivar certificate: The certificate which caused verificate failure.
    :type certificate: :class:`X509`
    """

    def __init__(self, message, certificate):
        super(X509StoreContextError, self).__init__(message)
        self.certificate = certificate


class X509StoreContext(object):
    """
    An X.509 store context.

    An :py:class:`X509StoreContext` is used to define some of the criteria for
    certificate verification.  The information encapsulated in this object
    includes, but is not limited to, a set of trusted certificates,
    verification parameters, and revoked certificates.

    .. note::

      Currently, one can only set the trusted certificates on an
      :py:class:`X509StoreContext`.  Future versions of pyOpenSSL will expose
      verification parameters and certificate revocation lists.

    :ivar _store_ctx: The underlying X509_STORE_CTX structure used by this
        instance.  It is dynamically allocated and automatically garbage
        collected.

    :ivar _store: See the ``store`` ``__init__`` parameter.

    :ivar _cert: See the ``certificate`` ``__init__`` parameter.

    :param X509Store store: The certificates which will be trusted for the
        purposes of any verifications.

    :param X509 certificate: The certificate to be verified.
    """

    def __init__(self, store, certificate):
        store_ctx = _lib.X509_STORE_CTX_new()
        self._store_ctx = _ffi.gc(store_ctx, _lib.X509_STORE_CTX_free)
        self._store = store
        self._cert = certificate
        # Make the store context available for use after instantiating this
        # class by initializing it now. Per testing, subsequent calls to
        # :py:meth:`_init` have no adverse affect.
        self._init()

    def _init(self):
        """
        Set up the store context for a subsequent verification operation.
        """
        ret = _lib.X509_STORE_CTX_init(
            self._store_ctx, self._store._store, self._cert._x509, _ffi.NULL
        )
        if ret <= 0:
            _raise_current_error()

    def _cleanup(self):
        """
        Internally cleans up the store context.

        The store context can then be reused with a new call to
        :py:meth:`_init`.
        """
        _lib.X509_STORE_CTX_cleanup(self._store_ctx)

    def _exception_from_context(self):
        """
        Convert an OpenSSL native context error failure into a Python
        exception.

        When a call to native OpenSSL X509_verify_cert fails, additional
        information about the failure can be obtained from the store context.
        """
        errors = [
            _lib.X509_STORE_CTX_get_error(self._store_ctx),
            _lib.X509_STORE_CTX_get_error_depth(self._store_ctx),
            _native(_ffi.string(_lib.X509_verify_cert_error_string(
                _lib.X509_STORE_CTX_get_error(self._store_ctx)))),
        ]
        # A context error should always be associated with a certificate, so we
        # expect this call to never return :class:`None`.
        _x509 = _lib.X509_STORE_CTX_get_current_cert(self._store_ctx)
        _cert = _lib.X509_dup(_x509)
        pycert = X509.__new__(X509)
        pycert._x509 = _ffi.gc(_cert, _lib.X509_free)
        return X509StoreContextError(errors, pycert)

    def set_store(self, store):
        """
        Set the context's trust store.

        .. versionadded:: 0.15

        :param X509Store store: The certificates which will be trusted for the
            purposes of any *future* verifications.
        """
        self._store = store

    def verify_certificate(self):
        """
        Verify a certificate in a context.

        .. versionadded:: 0.15

        :param store_ctx: The :py:class:`X509StoreContext` to verify.

        :raises X509StoreContextError: If an error occurred when validating a
          certificate in the context. Sets ``certificate`` attribute to
          indicate which certificate caused the error.
        """
        # Always re-initialize the store context in case
        # :py:meth:`verify_certificate` is called multiple times.
        self._init()
        ret = _lib.X509_verify_cert(self._store_ctx)
        self._cleanup()
        if ret <= 0:
            raise self._exception_from_context()


def load_certificate(type, buffer):
    """
    Load a certificate from a buffer

    :param type: The file type (one of FILETYPE_PEM, FILETYPE_ASN1)

    :param buffer: The buffer the certificate is stored in
    :type buffer: :py:class:`bytes`

    :return: The X509 object
    """
    if isinstance(buffer, _text_type):
        buffer = buffer.encode("ascii")

    bio = _new_mem_buf(buffer)

    if type == FILETYPE_PEM:
        x509 = _lib.PEM_read_bio_X509(bio, _ffi.NULL, _ffi.NULL, _ffi.NULL)
    elif type == FILETYPE_ASN1:
        x509 = _lib.d2i_X509_bio(bio, _ffi.NULL)
    else:
        raise ValueError(
            "type argument must be FILETYPE_PEM or FILETYPE_ASN1")

    if x509 == _ffi.NULL:
        _raise_current_error()

    cert = X509.__new__(X509)
    cert._x509 = _ffi.gc(x509, _lib.X509_free)
    return cert


def dump_certificate(type, cert):
    """
    Dump a certificate to a buffer

    :param type: The file type (one of FILETYPE_PEM, FILETYPE_ASN1, or
        FILETYPE_TEXT)
    :param cert: The certificate to dump
    :return: The buffer with the dumped certificate in
    """
    bio = _new_mem_buf()

    if type == FILETYPE_PEM:
        result_code = _lib.PEM_write_bio_X509(bio, cert._x509)
    elif type == FILETYPE_ASN1:
        result_code = _lib.i2d_X509_bio(bio, cert._x509)
    elif type == FILETYPE_TEXT:
        result_code = _lib.X509_print_ex(bio, cert._x509, 0, 0)
    else:
        raise ValueError(
            "type argument must be FILETYPE_PEM, FILETYPE_ASN1, or "
            "FILETYPE_TEXT")

    assert result_code == 1
    return _bio_to_string(bio)


def dump_publickey(type, pkey):
    """
    Dump a public key to a buffer.

    :param type: The file type (one of :data:`FILETYPE_PEM` or
        :data:`FILETYPE_ASN1`).
    :param PKey pkey: The public key to dump
    :return: The buffer with the dumped key in it.
    :rtype: bytes
    """
    bio = _new_mem_buf()
    if type == FILETYPE_PEM:
        write_bio = _lib.PEM_write_bio_PUBKEY
    elif type == FILETYPE_ASN1:
        write_bio = _lib.i2d_PUBKEY_bio
    else:
        raise ValueError("type argument must be FILETYPE_PEM or FILETYPE_ASN1")

    result_code = write_bio(bio, pkey._pkey)
    if result_code != 1:  # pragma: no cover
        _raise_current_error()

    return _bio_to_string(bio)


def dump_privatekey(type, pkey, cipher=None, passphrase=None):
    """
    Dump a private key to a buffer

    :param type: The file type (one of FILETYPE_PEM, FILETYPE_ASN1, or
        FILETYPE_TEXT)
    :param pkey: The PKey to dump
    :param cipher: (optional) if encrypted PEM format, the cipher to
                   use
    :param passphrase: (optional) if encrypted PEM format, this can be either
                       the passphrase to use, or a callback for providing the
                       passphrase.
    :return: The buffer with the dumped key in
    :rtype: :py:data:`bytes`
    """
    bio = _new_mem_buf()

    if cipher is not None:
        if passphrase is None:
            raise TypeError(
                "if a value is given for cipher "
                "one must also be given for passphrase")
        cipher_obj = _lib.EVP_get_cipherbyname(_byte_string(cipher))
        if cipher_obj == _ffi.NULL:
            raise ValueError("Invalid cipher name")
    else:
        cipher_obj = _ffi.NULL

    helper = _PassphraseHelper(type, passphrase)
    if type == FILETYPE_PEM:
        result_code = _lib.PEM_write_bio_PrivateKey(
            bio, pkey._pkey, cipher_obj, _ffi.NULL, 0,
            helper.callback, helper.callback_args)
        helper.raise_if_problem()
    elif type == FILETYPE_ASN1:
        result_code = _lib.i2d_PrivateKey_bio(bio, pkey._pkey)
    elif type == FILETYPE_TEXT:
        rsa = _lib.EVP_PKEY_get1_RSA(pkey._pkey)
        result_code = _lib.RSA_print(bio, rsa, 0)
        # TODO RSA_free(rsa)?
    else:
        raise ValueError(
            "type argument must be FILETYPE_PEM, FILETYPE_ASN1, or "
            "FILETYPE_TEXT")

    if result_code == 0:
        _raise_current_error()

    return _bio_to_string(bio)


def _X509_REVOKED_dup(original):
    copy = _lib.X509_REVOKED_new()
    if copy == _ffi.NULL:
        # TODO: This is untested.
        _raise_current_error()

    if original.serialNumber != _ffi.NULL:
        _lib.ASN1_INTEGER_free(copy.serialNumber)
        copy.serialNumber = _lib.ASN1_INTEGER_dup(original.serialNumber)

    if original.revocationDate != _ffi.NULL:
        _lib.ASN1_TIME_free(copy.revocationDate)
        copy.revocationDate = _lib.M_ASN1_TIME_dup(original.revocationDate)

    if original.extensions != _ffi.NULL:
        extension_stack = _lib.sk_X509_EXTENSION_new_null()
        for i in range(_lib.sk_X509_EXTENSION_num(original.extensions)):
            original_ext = _lib.sk_X509_EXTENSION_value(original.extensions, i)
            copy_ext = _lib.X509_EXTENSION_dup(original_ext)
            _lib.sk_X509_EXTENSION_push(extension_stack, copy_ext)
        copy.extensions = extension_stack

    copy.sequence = original.sequence
    return copy


class Revoked(object):
    """
    A certificate revocation.
    """
    # http://www.openssl.org/docs/apps/x509v3_config.html#CRL_distribution_points_
    # which differs from crl_reasons of crypto/x509v3/v3_enum.c that matches
    # OCSP_crl_reason_str.  We use the latter, just like the command line
    # program.
    _crl_reasons = [
        b"unspecified",
        b"keyCompromise",
        b"CACompromise",
        b"affiliationChanged",
        b"superseded",
        b"cessationOfOperation",
        b"certificateHold",
        # b"removeFromCRL",
    ]

    def __init__(self):
        revoked = _lib.X509_REVOKED_new()
        self._revoked = _ffi.gc(revoked, _lib.X509_REVOKED_free)

    def set_serial(self, hex_str):
        """
        Set the serial number.

        The serial number is formatted as a hexadecimal number encoded in
        ASCII.

        :param hex_str: The new serial number.
        :type hex_str: :py:class:`bytes`

        :return: :py:const:`None`
        """
        bignum_serial = _ffi.gc(_lib.BN_new(), _lib.BN_free)
        bignum_ptr = _ffi.new("BIGNUM**")
        bignum_ptr[0] = bignum_serial
        bn_result = _lib.BN_hex2bn(bignum_ptr, hex_str)
        if not bn_result:
            raise ValueError("bad hex string")

        asn1_serial = _ffi.gc(
            _lib.BN_to_ASN1_INTEGER(bignum_serial, _ffi.NULL),
            _lib.ASN1_INTEGER_free)
        _lib.X509_REVOKED_set_serialNumber(self._revoked, asn1_serial)

    def get_serial(self):
        """
        Get the serial number.

        The serial number is formatted as a hexadecimal number encoded in
        ASCII.

        :return: The serial number.
        :rtype: :py:class:`bytes`
        """
        bio = _new_mem_buf()

        result = _lib.i2a_ASN1_INTEGER(bio, self._revoked.serialNumber)
        if result < 0:
            # TODO: This is untested.
            _raise_current_error()

        return _bio_to_string(bio)

    def _delete_reason(self):
        stack = self._revoked.extensions
        for i in range(_lib.sk_X509_EXTENSION_num(stack)):
            ext = _lib.sk_X509_EXTENSION_value(stack, i)
            if _lib.OBJ_obj2nid(ext.object) == _lib.NID_crl_reason:
                _lib.X509_EXTENSION_free(ext)
                _lib.sk_X509_EXTENSION_delete(stack, i)
                break

    def set_reason(self, reason):
        """
        Set the reason of this revocation.

        If :py:data:`reason` is :py:const:`None`, delete the reason instead.

        :param reason: The reason string.
        :type reason: :py:class:`bytes` or :py:class:`NoneType`

        :return: :py:const:`None`

        .. seealso::

            :py:meth:`all_reasons`, which gives you a list of all supported
            reasons which you might pass to this method.
        """
        if reason is None:
            self._delete_reason()
        elif not isinstance(reason, bytes):
            raise TypeError("reason must be None or a byte string")
        else:
            reason = reason.lower().replace(b' ', b'')
            reason_code = [r.lower() for r in self._crl_reasons].index(reason)

            new_reason_ext = _lib.ASN1_ENUMERATED_new()
            if new_reason_ext == _ffi.NULL:
                # TODO: This is untested.
                _raise_current_error()
            new_reason_ext = _ffi.gc(new_reason_ext, _lib.ASN1_ENUMERATED_free)

            set_result = _lib.ASN1_ENUMERATED_set(new_reason_ext, reason_code)
            if set_result == _ffi.NULL:
                # TODO: This is untested.
                _raise_current_error()

            self._delete_reason()
            add_result = _lib.X509_REVOKED_add1_ext_i2d(
                self._revoked, _lib.NID_crl_reason, new_reason_ext, 0, 0)

            if not add_result:
                # TODO: This is untested.
                _raise_current_error()

    def get_reason(self):
        """
        Set the reason of this revocation.

        :return: The reason, or :py:const:`None` if there is none.
        :rtype: :py:class:`bytes` or :py:class:`NoneType`

        .. seealso::

            :py:meth:`all_reasons`, which gives you a list of all supported
            reasons this method might return.
        """
        extensions = self._revoked.extensions
        for i in range(_lib.sk_X509_EXTENSION_num(extensions)):
            ext = _lib.sk_X509_EXTENSION_value(extensions, i)
            if _lib.OBJ_obj2nid(ext.object) == _lib.NID_crl_reason:
                bio = _new_mem_buf()

                print_result = _lib.X509V3_EXT_print(bio, ext, 0, 0)
                if not print_result:
                    print_result = _lib.M_ASN1_OCTET_STRING_print(
                        bio, ext.value
                    )
                    if print_result == 0:
                        # TODO: This is untested.
                        _raise_current_error()

                return _bio_to_string(bio)

    def all_reasons(self):
        """
        Return a list of all the supported reason strings.

        This list is a copy; modifying it does not change the supported reason
        strings.

        :return: A list of reason strings.
        :rtype: :py:class:`list` of :py:class:`bytes`
        """
        return self._crl_reasons[:]

    def set_rev_date(self, when):
        """
        Set the revocation timestamp.

        :param when: The timestamp of the revocation, as ASN.1 GENERALIZEDTIME.
        :type when: :py:class:`bytes`
        :return: :py:const:`None`
        """
        return _set_asn1_time(self._revoked.revocationDate, when)

    def get_rev_date(self):
        """
        Get the revocation timestamp.

        :return: The timestamp of the revocation, as ASN.1 GENERALIZEDTIME.
        :rtype: :py:class:`bytes`
        """
        return _get_asn1_time(self._revoked.revocationDate)


class CRL(object):
    """
    A certificate revocation list.
    """

    def __init__(self):
        """
        Create a new empty certificate revocation list.
        """
        crl = _lib.X509_CRL_new()
        self._crl = _ffi.gc(crl, _lib.X509_CRL_free)

    def get_revoked(self):
        """
        Return the revocations in this certificate revocation list.

        These revocations will be provided by value, not by reference.
        That means it's okay to mutate them: it won't affect this CRL.

        :return: The revocations in this CRL.
        :rtype: :py:class:`tuple` of :py:class:`Revocation`
        """
        results = []
        revoked_stack = self._crl.crl.revoked
        for i in range(_lib.sk_X509_REVOKED_num(revoked_stack)):
            revoked = _lib.sk_X509_REVOKED_value(revoked_stack, i)
            revoked_copy = _X509_REVOKED_dup(revoked)
            pyrev = Revoked.__new__(Revoked)
            pyrev._revoked = _ffi.gc(revoked_copy, _lib.X509_REVOKED_free)
            results.append(pyrev)
        if results:
            return tuple(results)

    def add_revoked(self, revoked):
        """
        Add a revoked (by value not reference) to the CRL structure

        This revocation will be added by value, not by reference. That
        means it's okay to mutate it after adding: it won't affect
        this CRL.

        :param revoked: The new revocation.
        :type revoked: :class:`Revoked`

        :return: :py:const:`None`
        """
        copy = _X509_REVOKED_dup(revoked._revoked)
        if copy == _ffi.NULL:
            # TODO: This is untested.
            _raise_current_error()

        add_result = _lib.X509_CRL_add0_revoked(self._crl, copy)
        if add_result == 0:
            # TODO: This is untested.
            _raise_current_error()

    def export(self, cert, key, type=FILETYPE_PEM, days=100,
               digest=_UNSPECIFIED):
        """
        Export a CRL as a string.

        :param cert: The certificate used to sign the CRL.
        :type cert: :py:class:`X509`

        :param key: The key used to sign the CRL.
        :type key: :py:class:`PKey`

        :param type: The export format, either :py:data:`FILETYPE_PEM`,
            :py:data:`FILETYPE_ASN1`, or :py:data:`FILETYPE_TEXT`.

        :param int days: The number of days until the next update of this CRL.

        :param bytes digest: The name of the message digest to use (eg
            ``b"sha1"``).

        :return: :py:data:`bytes`
        """
        if not isinstance(cert, X509):
            raise TypeError("cert must be an X509 instance")
        if not isinstance(key, PKey):
            raise TypeError("key must be a PKey instance")
        if not isinstance(type, int):
            raise TypeError("type must be an integer")

        if digest is _UNSPECIFIED:
            _warn(
                "The default message digest (md5) is deprecated.  "
                "Pass the name of a message digest explicitly.",
                category=DeprecationWarning,
                stacklevel=2,
            )
            digest = b"md5"

        digest_obj = _lib.EVP_get_digestbyname(digest)
        if digest_obj == _ffi.NULL:
            raise ValueError("No such digest method")

        bio = _lib.BIO_new(_lib.BIO_s_mem())
        if bio == _ffi.NULL:
            # TODO: This is untested.
            _raise_current_error()

        # A scratch time object to give different values to different CRL
        # fields
        sometime = _lib.ASN1_TIME_new()
        if sometime == _ffi.NULL:
            # TODO: This is untested.
            _raise_current_error()

        _lib.X509_gmtime_adj(sometime, 0)
        _lib.X509_CRL_set_lastUpdate(self._crl, sometime)

        _lib.X509_gmtime_adj(sometime, days * 24 * 60 * 60)
        _lib.X509_CRL_set_nextUpdate(self._crl, sometime)

        _lib.X509_CRL_set_issuer_name(
            self._crl, _lib.X509_get_subject_name(cert._x509)
        )

        sign_result = _lib.X509_CRL_sign(self._crl, key._pkey, digest_obj)
        if not sign_result:
            _raise_current_error()

        return dump_crl(type, self)


CRLType = CRL


class PKCS7(object):
    def type_is_signed(self):
        """
        Check if this NID_pkcs7_signed object

        :return: True if the PKCS7 is of type signed
        """
        if _lib.PKCS7_type_is_signed(self._pkcs7):
            return True
        return False

    def type_is_enveloped(self):
        """
        Check if this NID_pkcs7_enveloped object

        :returns: True if the PKCS7 is of type enveloped
        """
        if _lib.PKCS7_type_is_enveloped(self._pkcs7):
            return True
        return False

    def type_is_signedAndEnveloped(self):
        """
        Check if this NID_pkcs7_signedAndEnveloped object

        :returns: True if the PKCS7 is of type signedAndEnveloped
        """
        if _lib.PKCS7_type_is_signedAndEnveloped(self._pkcs7):
            return True
        return False

    def type_is_data(self):
        """
        Check if this NID_pkcs7_data object

        :return: True if the PKCS7 is of type data
        """
        if _lib.PKCS7_type_is_data(self._pkcs7):
            return True
        return False

    def get_type_name(self):
        """
        Returns the type name of the PKCS7 structure

        :return: A string with the typename
        """
        nid = _lib.OBJ_obj2nid(self._pkcs7.type)
        string_type = _lib.OBJ_nid2sn(nid)
        return _ffi.string(string_type)

PKCS7Type = PKCS7


class PKCS12(object):
    """
    A PKCS #12 archive.
    """

    def __init__(self):
        self._pkey = None
        self._cert = None
        self._cacerts = None
        self._friendlyname = None

    def get_certificate(self):
        """
        Get the certificate in the PKCS #12 structure.

        :return: The certificate, or :py:const:`None` if there is none.
        :rtype: :py:class:`X509` or :py:const:`None`
        """
        return self._cert

    def set_certificate(self, cert):
        """
        Set the certificate in the PKCS #12 structure.

        :param cert: The new certificate, or :py:const:`None` to unset it.
        :type cert: :py:class:`X509` or :py:const:`None`

        :return: :py:const:`None`
        """
        if not isinstance(cert, X509):
            raise TypeError("cert must be an X509 instance")
        self._cert = cert

    def get_privatekey(self):
        """
        Get the private key in the PKCS #12 structure.

        :return: The private key, or :py:const:`None` if there is none.
        :rtype: :py:class:`PKey`
        """
        return self._pkey

    def set_privatekey(self, pkey):
        """
        Set the certificate portion of the PKCS #12 structure.

        :param pkey: The new private key, or :py:const:`None` to unset it.
        :type pkey: :py:class:`PKey` or :py:const:`None`

        :return: :py:const:`None`
        """
        if not isinstance(pkey, PKey):
            raise TypeError("pkey must be a PKey instance")
        self._pkey = pkey

    def get_ca_certificates(self):
        """
        Get the CA certificates in the PKCS #12 structure.

        :return: A tuple with the CA certificates in the chain, or
            :py:const:`None` if there are none.
        :rtype: :py:class:`tuple` of :py:class:`X509` or :py:const:`None`
        """
        if self._cacerts is not None:
            return tuple(self._cacerts)

    def set_ca_certificates(self, cacerts):
        """
        Replace or set the CA certificates within the PKCS12 object.

        :param cacerts: The new CA certificates, or :py:const:`None` to unset
            them.
        :type cacerts: An iterable of :py:class:`X509` or :py:const:`None`

        :return: :py:const:`None`
        """
        if cacerts is None:
            self._cacerts = None
        else:
            cacerts = list(cacerts)
            for cert in cacerts:
                if not isinstance(cert, X509):
                    raise TypeError(
                        "iterable must only contain X509 instances"
                    )
            self._cacerts = cacerts

    def set_friendlyname(self, name):
        """
        Set the friendly name in the PKCS #12 structure.

        :param name: The new friendly name, or :py:const:`None` to unset.
        :type name: :py:class:`bytes` or :py:const:`None`

        :return: :py:const:`None`
        """
        if name is None:
            self._friendlyname = None
        elif not isinstance(name, bytes):
            raise TypeError(
                "name must be a byte string or None (not %r)" % (name,)
            )
        self._friendlyname = name

    def get_friendlyname(self):
        """
        Get the friendly name in the PKCS# 12 structure.

        :returns: The friendly name,  or :py:const:`None` if there is none.
        :rtype: :py:class:`bytes` or :py:const:`None`
        """
        return self._friendlyname

    def export(self, passphrase=None, iter=2048, maciter=1):
        """
        Dump a PKCS12 object as a string.

        For more information, see the :c:func:`PKCS12_create` man page.

        :param passphrase: The passphrase used to encrypt the structure. Unlike
            some other passphrase arguments, this *must* be a string, not a
            callback.
        :type passphrase: :py:data:`bytes`

        :param iter: Number of times to repeat the encryption step.
        :type iter: :py:data:`int`

        :param maciter: Number of times to repeat the MAC step.
        :type maciter: :py:data:`int`

        :return: The string representation of the PKCS #12 structure.
        :rtype:
        """
        passphrase = _text_to_bytes_and_warn("passphrase", passphrase)

        if self._cacerts is None:
            cacerts = _ffi.NULL
        else:
            cacerts = _lib.sk_X509_new_null()
            cacerts = _ffi.gc(cacerts, _lib.sk_X509_free)
            for cert in self._cacerts:
                _lib.sk_X509_push(cacerts, cert._x509)

        if passphrase is None:
            passphrase = _ffi.NULL

        friendlyname = self._friendlyname
        if friendlyname is None:
            friendlyname = _ffi.NULL

        if self._pkey is None:
            pkey = _ffi.NULL
        else:
            pkey = self._pkey._pkey

        if self._cert is None:
            cert = _ffi.NULL
        else:
            cert = self._cert._x509

        pkcs12 = _lib.PKCS12_create(
            passphrase, friendlyname, pkey, cert, cacerts,
            _lib.NID_pbe_WithSHA1And3_Key_TripleDES_CBC,
            _lib.NID_pbe_WithSHA1And3_Key_TripleDES_CBC,
            iter, maciter, 0)
        if pkcs12 == _ffi.NULL:
            _raise_current_error()
        pkcs12 = _ffi.gc(pkcs12, _lib.PKCS12_free)

        bio = _new_mem_buf()
        _lib.i2d_PKCS12_bio(bio, pkcs12)
        return _bio_to_string(bio)


PKCS12Type = PKCS12


class NetscapeSPKI(object):
    """
    A Netscape SPKI object.
    """

    def __init__(self):
        spki = _lib.NETSCAPE_SPKI_new()
        self._spki = _ffi.gc(spki, _lib.NETSCAPE_SPKI_free)

    def sign(self, pkey, digest):
        """
        Sign the certificate request with this key and digest type.

        :param pkey: The private key to sign with.
        :type pkey: :py:class:`PKey`

        :param digest: The message digest to use.
        :type digest: :py:class:`bytes`

        :return: :py:const:`None`
        """
        if pkey._only_public:
            raise ValueError("Key has only public part")

        if not pkey._initialized:
            raise ValueError("Key is uninitialized")

        digest_obj = _lib.EVP_get_digestbyname(_byte_string(digest))
        if digest_obj == _ffi.NULL:
            raise ValueError("No such digest method")

        sign_result = _lib.NETSCAPE_SPKI_sign(
            self._spki, pkey._pkey, digest_obj
        )
        if not sign_result:
            # TODO: This is untested.
            _raise_current_error()

    def verify(self, key):
        """
        Verifies a signature on a certificate request.

        :param key: The public key that signature is supposedly from.
        :type pkey: :py:class:`PKey`

        :return: :py:const:`True` if the signature is correct.
        :rtype: :py:class:`bool`

        :raises Error: If the signature is invalid, or there was a problem
            verifying the signature.
        """
        answer = _lib.NETSCAPE_SPKI_verify(self._spki, key._pkey)
        if answer <= 0:
            _raise_current_error()
        return True

    def b64_encode(self):
        """
        Generate a base64 encoded representation of this SPKI object.

        :return: The base64 encoded string.
        :rtype: :py:class:`bytes`
        """
        encoded = _lib.NETSCAPE_SPKI_b64_encode(self._spki)
        result = _ffi.string(encoded)
        _lib.CRYPTO_free(encoded)
        return result

    def get_pubkey(self):
        """
        Get the public key of this certificate.

        :return: The public key.
        :rtype: :py:class:`PKey`
        """
        pkey = PKey.__new__(PKey)
        pkey._pkey = _lib.NETSCAPE_SPKI_get_pubkey(self._spki)
        if pkey._pkey == _ffi.NULL:
            # TODO: This is untested.
            _raise_current_error()
        pkey._pkey = _ffi.gc(pkey._pkey, _lib.EVP_PKEY_free)
        pkey._only_public = True
        return pkey

    def set_pubkey(self, pkey):
        """
        Set the public key of the certificate

        :param pkey: The public key
        :return: :py:const:`None`
        """
        set_result = _lib.NETSCAPE_SPKI_set_pubkey(self._spki, pkey._pkey)
        if not set_result:
            # TODO: This is untested.
            _raise_current_error()


NetscapeSPKIType = NetscapeSPKI


class _PassphraseHelper(object):
    def __init__(self, type, passphrase, more_args=False, truncate=False):
        if type != FILETYPE_PEM and passphrase is not None:
            raise ValueError(
                "only FILETYPE_PEM key format supports encryption"
            )
        self._passphrase = passphrase
        self._more_args = more_args
        self._truncate = truncate
        self._problems = []

    @property
    def callback(self):
        if self._passphrase is None:
            return _ffi.NULL
        elif isinstance(self._passphrase, bytes):
            return _ffi.NULL
        elif callable(self._passphrase):
            return _ffi.callback("pem_password_cb", self._read_passphrase)
        else:
            raise TypeError("Last argument must be string or callable")

    @property
    def callback_args(self):
        if self._passphrase is None:
            return _ffi.NULL
        elif isinstance(self._passphrase, bytes):
            return self._passphrase
        elif callable(self._passphrase):
            return _ffi.NULL
        else:
            raise TypeError("Last argument must be string or callable")

    def raise_if_problem(self, exceptionType=Error):
        try:
            _exception_from_error_queue(exceptionType)
        except exceptionType as e:
            from_queue = e
        if self._problems:
            raise self._problems[0]
        return from_queue

    def _read_passphrase(self, buf, size, rwflag, userdata):
        try:
            if self._more_args:
                result = self._passphrase(size, rwflag, userdata)
            else:
                result = self._passphrase(rwflag)
            if not isinstance(result, bytes):
                raise ValueError("String expected")
            if len(result) > size:
                if self._truncate:
                    result = result[:size]
                else:
                    raise ValueError(
                        "passphrase returned by callback is too long"
                    )
            for i in range(len(result)):
                buf[i] = result[i:i + 1]
            return len(result)
        except Exception as e:
            self._problems.append(e)
            return 0


def load_publickey(type, buffer):
    """
    Load a public key from a buffer.

    :param type: The file type (one of :data:`FILETYPE_PEM`,
        :data:`FILETYPE_ASN1`).
    :param buffer: The buffer the key is stored in.
    :type buffer: A Python string object, either unicode or bytestring.
    :return: The PKey object.
    :rtype: :class:`PKey`
    """
    if isinstance(buffer, _text_type):
        buffer = buffer.encode("ascii")

    bio = _new_mem_buf(buffer)

    if type == FILETYPE_PEM:
        evp_pkey = _lib.PEM_read_bio_PUBKEY(
            bio, _ffi.NULL, _ffi.NULL, _ffi.NULL)
    elif type == FILETYPE_ASN1:
        evp_pkey = _lib.d2i_PUBKEY_bio(bio, _ffi.NULL)
    else:
        raise ValueError("type argument must be FILETYPE_PEM or FILETYPE_ASN1")

    if evp_pkey == _ffi.NULL:
        _raise_current_error()

    pkey = PKey.__new__(PKey)
    pkey._pkey = _ffi.gc(evp_pkey, _lib.EVP_PKEY_free)
    return pkey


def load_privatekey(type, buffer, passphrase=None):
    """
    Load a private key from a buffer

    :param type: The file type (one of FILETYPE_PEM, FILETYPE_ASN1)
    :param buffer: The buffer the key is stored in
    :param passphrase: (optional) if encrypted PEM format, this can be
                       either the passphrase to use, or a callback for
                       providing the passphrase.

    :return: The PKey object
    """
    if isinstance(buffer, _text_type):
        buffer = buffer.encode("ascii")

    bio = _new_mem_buf(buffer)

    helper = _PassphraseHelper(type, passphrase)
    if type == FILETYPE_PEM:
        evp_pkey = _lib.PEM_read_bio_PrivateKey(
            bio, _ffi.NULL, helper.callback, helper.callback_args)
        helper.raise_if_problem()
    elif type == FILETYPE_ASN1:
        evp_pkey = _lib.d2i_PrivateKey_bio(bio, _ffi.NULL)
    else:
        raise ValueError("type argument must be FILETYPE_PEM or FILETYPE_ASN1")

    if evp_pkey == _ffi.NULL:
        _raise_current_error()

    pkey = PKey.__new__(PKey)
    pkey._pkey = _ffi.gc(evp_pkey, _lib.EVP_PKEY_free)
    return pkey


def dump_certificate_request(type, req):
    """
    Dump a certificate request to a buffer

    :param type: The file type (one of FILETYPE_PEM, FILETYPE_ASN1)
    :param req: The certificate request to dump
    :return: The buffer with the dumped certificate request in
    """
    bio = _new_mem_buf()

    if type == FILETYPE_PEM:
        result_code = _lib.PEM_write_bio_X509_REQ(bio, req._req)
    elif type == FILETYPE_ASN1:
        result_code = _lib.i2d_X509_REQ_bio(bio, req._req)
    elif type == FILETYPE_TEXT:
        result_code = _lib.X509_REQ_print_ex(bio, req._req, 0, 0)
    else:
        raise ValueError(
            "type argument must be FILETYPE_PEM, FILETYPE_ASN1, or "
            "FILETYPE_TEXT"
        )

    if result_code == 0:
        # TODO: This is untested.
        _raise_current_error()

    return _bio_to_string(bio)


def load_certificate_request(type, buffer):
    """
    Load a certificate request from a buffer

    :param type: The file type (one of FILETYPE_PEM, FILETYPE_ASN1)
    :param buffer: The buffer the certificate request is stored in
    :return: The X509Req object
    """
    if isinstance(buffer, _text_type):
        buffer = buffer.encode("ascii")

    bio = _new_mem_buf(buffer)

    if type == FILETYPE_PEM:
        req = _lib.PEM_read_bio_X509_REQ(bio, _ffi.NULL, _ffi.NULL, _ffi.NULL)
    elif type == FILETYPE_ASN1:
        req = _lib.d2i_X509_REQ_bio(bio, _ffi.NULL)
    else:
        raise ValueError("type argument must be FILETYPE_PEM or FILETYPE_ASN1")

    if req == _ffi.NULL:
        # TODO: This is untested.
        _raise_current_error()

    x509req = X509Req.__new__(X509Req)
    x509req._req = _ffi.gc(req, _lib.X509_REQ_free)
    return x509req


def sign(pkey, data, digest):
    """
    Sign data with a digest

    :param pkey: Pkey to sign with
    :param data: data to be signed
    :param digest: message digest to use
    :return: signature
    """
    data = _text_to_bytes_and_warn("data", data)

    digest_obj = _lib.EVP_get_digestbyname(_byte_string(digest))
    if digest_obj == _ffi.NULL:
        raise ValueError("No such digest method")

    md_ctx = _ffi.new("EVP_MD_CTX*")
    md_ctx = _ffi.gc(md_ctx, _lib.EVP_MD_CTX_cleanup)

    _lib.EVP_SignInit(md_ctx, digest_obj)
    _lib.EVP_SignUpdate(md_ctx, data, len(data))

    signature_buffer = _ffi.new("unsigned char[]", 512)
    signature_length = _ffi.new("unsigned int*")
    signature_length[0] = len(signature_buffer)
    final_result = _lib.EVP_SignFinal(
        md_ctx, signature_buffer, signature_length, pkey._pkey)

    if final_result != 1:
        # TODO: This is untested.
        _raise_current_error()

    return _ffi.buffer(signature_buffer, signature_length[0])[:]


def verify(cert, signature, data, digest):
    """
    Verify a signature.

    :param cert: signing certificate (X509 object)
    :param signature: signature returned by sign function
    :param data: data to be verified
    :param digest: message digest to use
    :return: :py:const:`None` if the signature is correct, raise exception
        otherwise
    """
    data = _text_to_bytes_and_warn("data", data)

    digest_obj = _lib.EVP_get_digestbyname(_byte_string(digest))
    if digest_obj == _ffi.NULL:
        raise ValueError("No such digest method")

    pkey = _lib.X509_get_pubkey(cert._x509)
    if pkey == _ffi.NULL:
        # TODO: This is untested.
        _raise_current_error()
    pkey = _ffi.gc(pkey, _lib.EVP_PKEY_free)

    md_ctx = _ffi.new("EVP_MD_CTX*")
    md_ctx = _ffi.gc(md_ctx, _lib.EVP_MD_CTX_cleanup)

    _lib.EVP_VerifyInit(md_ctx, digest_obj)
    _lib.EVP_VerifyUpdate(md_ctx, data, len(data))
    verify_result = _lib.EVP_VerifyFinal(
        md_ctx, signature, len(signature), pkey
    )

    if verify_result != 1:
        _raise_current_error()


def dump_crl(type, crl):
    """
    Dump a certificate revocation list to a buffer.

    :param type: The file type (one of ``FILETYPE_PEM``, ``FILETYPE_ASN1``, or
        ``FILETYPE_TEXT``).
    :param CRL crl: The CRL to dump.

    :return: The buffer with the CRL.
    :rtype: :data:`bytes`
    """
    bio = _new_mem_buf()

    if type == FILETYPE_PEM:
        ret = _lib.PEM_write_bio_X509_CRL(bio, crl._crl)
    elif type == FILETYPE_ASN1:
        ret = _lib.i2d_X509_CRL_bio(bio, crl._crl)
    elif type == FILETYPE_TEXT:
        ret = _lib.X509_CRL_print(bio, crl._crl)
    else:
        raise ValueError(
            "type argument must be FILETYPE_PEM, FILETYPE_ASN1, or "
            "FILETYPE_TEXT")

    assert ret == 1
    return _bio_to_string(bio)


def load_crl(type, buffer):
    """
    Load a certificate revocation list from a buffer

    :param type: The file type (one of FILETYPE_PEM, FILETYPE_ASN1)
    :param buffer: The buffer the CRL is stored in

    :return: The PKey object
    """
    if isinstance(buffer, _text_type):
        buffer = buffer.encode("ascii")

    bio = _new_mem_buf(buffer)

    if type == FILETYPE_PEM:
        crl = _lib.PEM_read_bio_X509_CRL(bio, _ffi.NULL, _ffi.NULL, _ffi.NULL)
    elif type == FILETYPE_ASN1:
        crl = _lib.d2i_X509_CRL_bio(bio, _ffi.NULL)
    else:
        raise ValueError("type argument must be FILETYPE_PEM or FILETYPE_ASN1")

    if crl == _ffi.NULL:
        _raise_current_error()

    result = CRL.__new__(CRL)
    result._crl = crl
    return result


def load_pkcs7_data(type, buffer):
    """
    Load pkcs7 data from a buffer

    :param type: The file type (one of FILETYPE_PEM or FILETYPE_ASN1)
    :param buffer: The buffer with the pkcs7 data.
    :return: The PKCS7 object
    """
    if isinstance(buffer, _text_type):
        buffer = buffer.encode("ascii")

    bio = _new_mem_buf(buffer)

    if type == FILETYPE_PEM:
        pkcs7 = _lib.PEM_read_bio_PKCS7(bio, _ffi.NULL, _ffi.NULL, _ffi.NULL)
    elif type == FILETYPE_ASN1:
        pkcs7 = _lib.d2i_PKCS7_bio(bio, _ffi.NULL)
    else:
        # TODO: This is untested.
        _raise_current_error()
        raise ValueError("type argument must be FILETYPE_PEM or FILETYPE_ASN1")

    if pkcs7 == _ffi.NULL:
        _raise_current_error()

    pypkcs7 = PKCS7.__new__(PKCS7)
    pypkcs7._pkcs7 = _ffi.gc(pkcs7, _lib.PKCS7_free)
    return pypkcs7


def load_pkcs12(buffer, passphrase=None):
    """
    Load a PKCS12 object from a buffer

    :param buffer: The buffer the certificate is stored in
    :param passphrase: (Optional) The password to decrypt the PKCS12 lump
    :returns: The PKCS12 object
    """
    passphrase = _text_to_bytes_and_warn("passphrase", passphrase)

    if isinstance(buffer, _text_type):
        buffer = buffer.encode("ascii")

    bio = _new_mem_buf(buffer)

    # Use null passphrase if passphrase is None or empty string. With PKCS#12
    # password based encryption no password and a zero length password are two
    # different things, but OpenSSL implementation will try both to figure out
    # which one works.
    if not passphrase:
        passphrase = _ffi.NULL

    p12 = _lib.d2i_PKCS12_bio(bio, _ffi.NULL)
    if p12 == _ffi.NULL:
        _raise_current_error()
    p12 = _ffi.gc(p12, _lib.PKCS12_free)

    pkey = _ffi.new("EVP_PKEY**")
    cert = _ffi.new("X509**")
    cacerts = _ffi.new("Cryptography_STACK_OF_X509**")

    parse_result = _lib.PKCS12_parse(p12, passphrase, pkey, cert, cacerts)
    if not parse_result:
        _raise_current_error()

    cacerts = _ffi.gc(cacerts[0], _lib.sk_X509_free)

    # openssl 1.0.0 sometimes leaves an X509_check_private_key error in the
    # queue for no particular reason.  This error isn't interesting to anyone
    # outside this function.  It's not even interesting to us.  Get rid of it.
    try:
        _raise_current_error()
    except Error:
        pass

    if pkey[0] == _ffi.NULL:
        pykey = None
    else:
        pykey = PKey.__new__(PKey)
        pykey._pkey = _ffi.gc(pkey[0], _lib.EVP_PKEY_free)

    if cert[0] == _ffi.NULL:
        pycert = None
        friendlyname = None
    else:
        pycert = X509.__new__(X509)
        pycert._x509 = _ffi.gc(cert[0], _lib.X509_free)

        friendlyname_length = _ffi.new("int*")
        friendlyname_buffer = _lib.X509_alias_get0(
            cert[0], friendlyname_length
        )
        friendlyname = _ffi.buffer(
            friendlyname_buffer, friendlyname_length[0]
        )[:]
        if friendlyname_buffer == _ffi.NULL:
            friendlyname = None

    pycacerts = []
    for i in range(_lib.sk_X509_num(cacerts)):
        pycacert = X509.__new__(X509)
        pycacert._x509 = _lib.sk_X509_value(cacerts, i)
        pycacerts.append(pycacert)
    if not pycacerts:
        pycacerts = None

    pkcs12 = PKCS12.__new__(PKCS12)
    pkcs12._pkey = pykey
    pkcs12._cert = pycert
    pkcs12._cacerts = pycacerts
    pkcs12._friendlyname = friendlyname
    return pkcs12


# There are no direct unit tests for this initialization.  It is tested
# indirectly since it is necessary for functions like dump_privatekey when
# using encryption.
#
# Thus OpenSSL.test.test_crypto.FunctionTests.test_dump_privatekey_passphrase
# and some other similar tests may fail without this (though they may not if
# the Python runtime has already done some initialization of the underlying
# OpenSSL library (and is linked against the same one that cryptography is
# using)).
_lib.OpenSSL_add_all_algorithms()

# This is similar but exercised mainly by exception_from_error_queue.  It calls
# both ERR_load_crypto_strings() and ERR_load_SSL_strings().
_lib.SSL_load_error_strings()


# Set the default string mask to match OpenSSL upstream (since 2005) and
# RFC5280 recommendations.
_lib.ASN1_STRING_set_default_mask_asc(b'utf8only')
