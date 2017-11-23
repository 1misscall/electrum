import sys
import struct
import traceback
sys.path.insert(0, "lib/ln")
from .ln import rpc_pb2
import os

from jsonrpclib.SimpleJSONRPCServer import SimpleJSONRPCServer
from jsonrpclib import Server
from google.protobuf import json_format
import binascii
import ecdsa.util
import ecdsa.curves
import hashlib
from .bitcoin import EC_KEY
from . import bitcoin
from . import transaction

import queue

from .util import ThreadJob
import socks
import socket
import time

WALLET = None
NETWORK = None
CONFIG = None
locked = set()


def SetHdSeed(json):
    req = rpc_pb2.SetHdSeedRequest()
    json_format.Parse(json, req)
    print("set hdseed unimplemented", req.hdSeed)
    m = rpc_pb2.SetHdSeedResponse()
    msg = json_format.MessageToJson(m)
    return msg


def ConfirmedBalance(json):
    request = rpc_pb2.ConfirmedBalanceRequest()
    json_format.Parse(json, request)
    m = rpc_pb2.ConfirmedBalanceResponse()
    confs = request.confirmations
    witness = request.witness  # bool

    WALLET.synchronize()
    WALLET.wait_until_synchronized()

    m.amount = sum(WALLET.get_balance())
    msg = json_format.MessageToJson(m)
    return msg


def NewAddress(json):
    request = rpc_pb2.NewAddressRequest()
    json_format.Parse(json, request)
    m = rpc_pb2.NewAddressResponse()
    if request.type == rpc_pb2.WITNESS_PUBKEY_HASH:
        m.address = WALLET.get_unused_address()
    elif request.type == rpc_pb2.NESTED_PUBKEY_HASH:
        assert False, "cannot handle nested-pubkey-hash address type generation yet"
    elif request.type == rpc_pb2.PUBKEY_HASH:
        assert False, "cannot handle pubkey_hash generation yet"
    else:
        assert False, "unknown address type"
    msg = json_format.MessageToJson(m)
    return msg


def FetchRootKey(json):
    request = rpc_pb2.FetchRootKeyRequest()
    json_format.Parse(json, request)
    m = rpc_pb2.FetchRootKeyResponse()
    m.rootKey = os.urandom(32) # TODO derive from wallet
    msg = json_format.MessageToJson(m)
    return msg


cl = rpc_pb2.ListUnspentWitnessRequest

assert rpc_pb2.WITNESS_PUBKEY_HASH is not None


def ListUnspentWitness(json):
    req = cl()
    json_format.Parse(json, req)
    confs = req.minConfirmations #TODO regard this

    WALLET.synchronize()
    WALLET.wait_until_synchronized()

    unspent = WALLET.get_utxos()
    m = rpc_pb2.ListUnspentWitnessResponse()
    for utxo in unspent:
        # print(utxo)
        # example:
        # {'prevout_n': 0,
        #  'address': 'sb1qt52ccplvtpehz7qvvqft2udf2eaqvfsal08xre',
        #  'prevout_hash': '0d4caccd6e8a906c8ca22badf597c4dedc6dd7839f3cac3137f8f29212099882',
        #  'coinbase': False,
        #  'height': 326,
        #  'value': 400000000}

        global locked
        if (utxo["prevout_hash"], utxo["prevout_n"]) in locked:
            print("SKIPPING LOCKED OUTPOINT", utxo["prevout_hash"])
            continue
        towire = m.utxos.add()
        towire.addressType = rpc_pb2.WITNESS_PUBKEY_HASH
        towire.redeemScript = b""
        towire.pkScript = b""
        towire.witnessScript = bytes(bytearray.fromhex(
            bitcoin.address_to_script(utxo["address"])))
        towire.value = utxo["value"]
        towire.outPoint.hash = utxo["prevout_hash"]
        towire.outPoint.index = utxo["prevout_n"]
    return json_format.MessageToJson(m)


i = 0

usedAddresses = set()

def NewRawKey(json):
    global i
    addresses = WALLET.get_unused_addresses()
    res = rpc_pb2.NewRawKeyResponse()
    pubk = None
    assert len(set(addresses) - usedAddresses) > 0, "used all addresses!"
    while pubk is None:
      i = i + 1
      if i > len(addresses) - 1:
          i = 0
      # TODO do not reuse keys!!!!!!!!!!!!!!!!
      # find out when get_unused_addresses marks an address used...
      if addresses[i] not in usedAddresses:
        pubk = addresses[i]
        usedAddresses.add(pubk)
    res.publicKey = bytes(bytearray.fromhex(WALLET.get_public_keys(pubk)[0]))
    return json_format.MessageToJson(res)


def LockOutpoint(json):
    req = rpc_pb2.LockOutpointRequest()
    json_format.Parse(json, req)
    global locked
    locked.add((req.outpoint.hash, req.outpoint.index))


def UnlockOutpoint(json):
    req = rpc_pb2.UnlockOutpointRequest()
    json_format.Parse(json, req)
    global locked
    # throws KeyError if not existing. Use .discard() if we do not care
    locked.remove((req.outpoint.hash, req.outpoint.index))

HEIGHT = None

def ListTransactionDetails(json):
    global HEIGHT
    global WALLET
    global NETWORK
    WALLET.synchronize()
    WALLET.wait_until_synchronized()
    if HEIGHT is None:
        HEIGHT = WALLET.get_local_height()
    else:
        assert HEIGHT != WALLET.get_local_height(), ("old height " + str(HEIGHT), "new height " + str(WALLET.get_local_height()))
        HEIGHT = WALLET.get_local_height()
    m = rpc_pb2.ListTransactionDetailsResponse()
    for tx_hash, height, conf, timestamp, delta, balance in WALLET.get_history():
        if height == 0:
          print("WARNING", tx_hash, "has zero height!")
        detail = m.details.add()
        detail.hash = tx_hash
        detail.value = delta
        detail.numConfirmations = conf
        detail.blockHash = NETWORK.blockchain().get_hash(height)
        detail.blockHeight = height
        detail.timestamp = timestamp
        detail.totalFees = 1337 # TODO
    return json_format.MessageToJson(m)

def FetchInputInfo(json):
    req = rpc_pb2.FetchInputInfoRequest()
    json_format.Parse(json, req)
    has = req.outPoint.hash
    idx = req.outPoint.index
    txoinfo = WALLET.txo.get(has, {})
    m = rpc_pb2.FetchInputInfoResponse()
    if has in WALLET.transactions:
        tx = WALLET.transactions[has]
        m.mine = True
    else:
        tx = WALLET.get_input_tx(has)
        print("did not find tx with hash", has)
        print("tx", tx)

        m.mine = False
        return json_format.MessageToJson(m)
    outputs = tx.outputs()
    assert {bitcoin.TYPE_SCRIPT: "SCRIPT", bitcoin.TYPE_ADDRESS: "ADDRESS",
            bitcoin.TYPE_PUBKEY: "PUBKEY"}[outputs[idx][0]] == "ADDRESS"
    scr = transaction.Transaction.pay_script(outputs[idx][0], outputs[idx][1])
    m.txOut.value = outputs[idx][2]  # type, addr, val
    m.txOut.pkScript = bytes(bytearray.fromhex(scr))
    msg = json_format.MessageToJson(m)
    return msg

def SendOutputs(json):
    global NETWORK, WALLET, CONFIG

    req = rpc_pb2.SendOutputsRequest()
    json_format.Parse(json, req)

    m = rpc_pb2.SendOutputsResponse()

    elecOutputs = [(bitcoin.TYPE_SCRIPT, binascii.hexlify(txout.pkScript).decode("utf-8"), txout.value) for txout in req.outputs]

    tx = None
    try:
        #                outputs,     password, config, fee
        tx = WALLET.mktx(elecOutputs, None,     CONFIG, 1000)
    except Exception as e:
        m.success = False
        m.error = str(e)
        m.resultHash = ""
        return json_format.MessageToJson(m)

    suc, has = NETWORK.broadcast(tx)
    if not suc:
        m.success = False
        m.error = "electrum/lightning/SendOutputs: Could not broadcast: " + str(has)
        m.resultHash = ""
        return json_format.MessageToJson(m)
    m.success = True
    m.error = ""
    m.resultHash = tx.txid()
    return json_format.MessageToJson(m)

def IsSynced(json):
    global NETWORK
    local_height, server_height = NETWORK.get_status_value("updated")
    synced = NETWORK.is_up_to_date() and local_height == server_height
    m = rpc_pb2.IsSyncedResponse()
    m.synced = synced
    return json_format.MessageToJson(m)

def SignMessage(json):
    req = rpc_pb2.SignMessageRequest()
    json_format.Parse(json, req)
    m = rpc_pb2.SignMessageResponse()

    address = None
    for adr in usedAddresses:
      if req.pubKey == bytes(bytearray.fromhex(WALLET.get_public_keys(adr)[0])):
        address = adr
        break

    assert address is not None, "did not find address in list of addresses given out by NewRawKey"

    pri, _ = WALLET.export_private_key(address, None)
    typ, pri, compressed = bitcoin.deserialize_privkey(pri)
    pri = EC_KEY(pri)

    m.signature = pri.sign(bitcoin.Hash(req.messageToBeSigned), ecdsa.util.sigencode_der)
    m.error = ""
    m.success = True
    return json_format.MessageToJson(m)

def wrap(fun):
    def wrapped(*args, **kwargs):
        try:
            return fun(*args, **kwargs)
        except Exception as e:
            # jsonRPC doesn't report the full trace, so we do it
            traceback.print_exc()
            raise e
    # this is needed for the jsonRPC library to know which method this is.
    # there is an alternative register_function call with a string passed.
    wrapped.__name__ = fun.__name__
    return wrapped

def get_server(port):
    server = SimpleJSONRPCServer(('localhost', port))
    server.register_function(wrap(FetchRootKey))
    server.register_function(wrap(ConfirmedBalance))
    server.register_function(wrap(NewAddress))
    server.register_function(wrap(ListUnspentWitness))
    server.register_function(wrap(SetHdSeed))
    server.register_function(wrap(NewRawKey))
    server.register_function(wrap(FetchInputInfo))
    server.register_function(wrap(ComputeInputScript))
    server.register_function(wrap(SignOutputRaw))
    server.register_function(wrap(PublishTransaction))
    server.register_function(wrap(LockOutpoint))
    server.register_function(wrap(UnlockOutpoint))
    server.register_function(wrap(ListTransactionDetails))
    server.register_function(wrap(SendOutputs))
    server.register_function(wrap(IsSynced))
    server.register_function(wrap(SignMessage))
    return server

def LEtobytes(x, l):
    if l == 2:
        fmt = "<H"
    elif l == 4:
        fmt = "<I"
    elif l == 8:
        fmt = "<Q"
    else:
        assert False, "invalid format for LEtobytes"
    return struct.pack(fmt, x)


def toint(x):
    if len(x) == 1:
        return ord(x)
    elif len(x) == 2:
        fmt = ">H"
    elif len(x) == 4:
        fmt = ">I"
    elif len(x) == 8:
        fmt = ">Q"
    else:
        assert False, "invalid length for toint(): " + str(len(x))
    return struct.unpack(fmt, x)[0]


class SignDescriptor(object):
    def __init__(self, pubKey=None, sigHashes=None, inputIndex=None, singleTweak=None, hashType=None, doubleTweak=None, witnessScript=None, output=None):
        self.pubKey = pubKey
        self.sigHashes = sigHashes
        self.inputIndex = inputIndex
        self.singleTweak = singleTweak
        self.hashType = hashType
        self.doubleTweak = doubleTweak
        self.witnessScript = witnessScript
        self.output = output

    def __str__(self):
        return '%s(%s)' % (
            type(self).__name__,
            ', '.join('%s=%s' % item for item in vars(self).items())
        )


class TxSigHashes(object):
    def __init__(self, hashOutputs=None, hashSequence=None, hashPrevOuts=None):
        self.hashOutputs = hashOutputs
        self.hashSequence = hashSequence
        self.hashPrevOuts = hashPrevOuts


class Output(object):
    def __init__(self, value=None, pkScript=None):
        assert value is not None and pkScript is not None
        self.value = value
        self.pkScript = pkScript


class InputScript(object):
    def __init__(self, scriptSig, witness):
        assert witness is None or type(witness[0]) is type(bytes([]))
        assert type(scriptSig) is type(bytes([]))
        self.scriptSig = scriptSig
        self.witness = witness


def tweakPrivKey(basePriv, commitTweak):
    tweakInt = int.from_bytes(commitTweak, byteorder="big")
    tweakInt += basePriv.secret # D is secret
    tweakInt %= ecdsa.curves.SECP256k1.generator.order()
    return EC_KEY(tweakInt.to_bytes(33, 'big')) # TODO find out if 33 bytes are necessary. private keys are usually only 32 bytes

def singleTweakBytes(commitPoint, basePoint):
    m = hashlib.sha256()
    m.update(bytearray.fromhex(commitPoint))
    m.update(bytearray.fromhex(basePoint))
    return m.digest()

def deriveRevocationPrivKey(revokeBasePriv, commitSecret):
    revokeTweakBytes = singleTweakBytes(revokeBasePriv.get_public_key(True),
                                        commitSecret.get_public_key(True))
    revokeTweakInt = int.from_bytes(revokeTweakBytes, byteorder="big")

    commitTweakBytes = singleTweakBytes(commitSecret.get_public_key(True),
                                        revokeBasePriv.get_public_key(True))
    commitTweakInt = int.from_bytes(commitTweakBytes, byteorder="big")

    revokeHalfPriv = revokeTweakInt * revokeBasePriv.secret # D is secret
    commitHalfPriv = commitTweakInt * commitSecret.secret

    revocationPriv = revokeHalfPriv + commitHalfPriv
    revocationPriv %= ecdsa.curves.SECP256k1.generator.order()

    return EC_KEY(revocationPriv.to_bytes(33, byteorder="big")) # TODO find out if 33 bytes are necessary. private keys are usually only 32 bytes


def maybeTweakPrivKey(signdesc, pri):
    if len(signdesc.singleTweak) > 0:
        return tweakPrivKey(pri, signdesc.singleTweak)
    elif len(signdesc.doubleTweak) > 0:
        return deriveRevocationPrivKey(pri, EC_KEY(signdesc.doubleTweak))
    else:
        return pri


def isWitnessPubKeyHash(script):
    if len(script) != 2:
        return False
    haveop0 = (transaction.opcodes.OP_0 == script[0][0])
    haveopdata20 = (20 == script[1][0])
    return haveop0 and haveopdata20

#// calcWitnessSignatureHash computes the sighash digest of a transaction's
#// segwit input using the new, optimized digest calculation algorithm defined
#// in BIP0143: https://github.com/bitcoin/bips/blob/master/bip-0143.mediawiki.
#// This function makes use of pre-calculated sighash fragments stored within
#// the passed HashCache to eliminate duplicate hashing computations when
#// calculating the final digest, reducing the complexity from O(N^2) to O(N).
#// Additionally, signatures now cover the input value of the referenced unspent
#// output. This allows offline, or hardware wallets to compute the exact amount
#// being spent, in addition to the final transaction fee. In the case the
#// wallet if fed an invalid input amount, the real sighash will differ causing
#// the produced signature to be invalid.


def calcWitnessSignatureHash(original, sigHashes, hashType, tx, idx, amt):
    assert len(original) != 0
    decoded = transaction.deserialize(binascii.hexlify(tx).decode("utf-8"))
    if idx > len(decoded["inputs"]) - 1:
        raise Exception("invalid inputIndex")
    txin = decoded["inputs"][idx]
    #tohash = transaction.Transaction.serialize_witness(txin)
    sigHash = LEtobytes(decoded["version"], 4)
    if toint(hashType) & toint(sigHashAnyOneCanPay) == 0:
        sigHash += bytes(bytearray.fromhex(sigHashes.hashPrevOuts))[::-1]
    else:
        sigHash += b"\x00" * 32

    if toint(hashType) & toint(sigHashAnyOneCanPay) == 0 and toint(hashType) & toint(sigHashMask) != toint(sigHashSingle) and toint(hashType) & toint(sigHashMask) != toint(sigHashNone):
        sigHash += bytes(bytearray.fromhex(sigHashes.hashSequence))[::-1]
    else:
        sigHash += b"\x00" * 32

    sigHash += bytes(bytearray.fromhex(txin["prevout_hash"]))[::-1]
    sigHash += LEtobytes(txin["prevout_n"], 4)
    # byte 72

    subscript = list(transaction.script_GetOp(original))
    if isWitnessPubKeyHash(subscript):
        sigHash += b"\x19"
        sigHash += bytes([transaction.opcodes.OP_DUP])
        sigHash += bytes([transaction.opcodes.OP_HASH160])
        sigHash += b"\x14"  # 20 bytes
        assert len(subscript) == 2, subscript
        opcode, data, length = subscript[1]
        sigHash += data
        sigHash += bytes([transaction.opcodes.OP_EQUALVERIFY])
        sigHash += bytes([transaction.opcodes.OP_CHECKSIG])
    else:
        # For p2wsh outputs, and future outputs, the script code is
        # the original script, with all code separators removed,
        # serialized with a var int length prefix.

        assert len(sigHash) == 104, len(sigHash)
        sigHash += bytes(bytearray.fromhex(bitcoin.var_int(len(original))))
        assert len(sigHash) == 105, len(sigHash)

        sigHash += original

    sigHash += LEtobytes(amt, 8)
    sigHash += LEtobytes(txin["sequence"], 4)

    if toint(hashType) & toint(sigHashSingle) != toint(sigHashSingle) and toint(hashType) & toint(sigHashNone) != toint(sigHashNone):
        sigHash += bytes(bytearray.fromhex(sigHashes.hashOutputs))[::-1]
    elif toint(hashtype) & toint(sigHashMask) == toint(sigHashSingle) and idx < len(decoded["outputs"]):
        raise Exception("TODO 1")
    else:
        raise Exception("TODO 2")

    sigHash += LEtobytes(decoded["lockTime"], 4)
    sigHash += LEtobytes(toint(hashType), 4)

    return transaction.Hash(sigHash)

#// RawTxInWitnessSignature returns the serialized ECDA signature for the input
#// idx of the given transaction, with the hashType appended to it. This
#// function is identical to RawTxInSignature, however the signature generated
#// signs a new sighash digest defined in BIP0143.
# func RawTxInWitnessSignature(tx *MsgTx, sigHashes *TxSigHashes, idx int,
#  amt int64, subScript []byte, hashType SigHashType,
#  key *btcec.PrivateKey) ([]byte, error) {


def rawTxInWitnessSignature(tx, sigHashes, idx, amt, subscript, hashType, key):
    digest = calcWitnessSignatureHash(
        subscript, sigHashes, hashType, tx, idx, amt)
    return key.sign(digest, sigencode=ecdsa.util.sigencode_der) + hashType

# WitnessSignature creates an input witness stack for tx to spend BTC sent
# from a previous output to the owner of privKey using the p2wkh script
# template. The passed transaction must contain all the inputs and outputs as
# dictated by the passed hashType. The signature generated observes the new
# transaction digest algorithm defined within BIP0143.
def witnessSignature(tx, sigHashes, idx, amt, subscript, hashType, privKey, compress):
    sig = rawTxInWitnessSignature(
        tx, sigHashes, idx, amt, subscript, hashType, privKey)

    pkData = bytes(bytearray.fromhex(
        privKey.get_public_key(compressed=compress)))

    return sig, pkData


sigHashMask = b"\x1f"

sigHashAll = b"\x01"
sigHashNone = b"\x02"
sigHashSingle = b"\x03"
sigHashAnyOneCanPay = b"\x80"

test = rpc_pb2.ComputeInputScriptResponse()

test.witnessScript.append(b"\x01")
test.witnessScript.append(b"\x02")


def SignOutputRaw(json):
    req = rpc_pb2.SignOutputRawRequest()
    json_format.Parse(json, req)

    assert len(req.signDesc.pubKey) in [33, 0]
    assert len(req.signDesc.doubleTweak) in [32, 0]
    assert len(req.signDesc.sigHashes.hashPrevOuts) == 64
    assert len(req.signDesc.sigHashes.hashSequence) == 64
    assert len(req.signDesc.sigHashes.hashOutputs) == 64

    m = rpc_pb2.SignOutputRawResponse()

    m.signature = signOutputRaw(req.tx, req.signDesc)

    msg = json_format.MessageToJson(m)
    return msg


def signOutputRaw(tx, signDesc):
    adr = bitcoin.pubkey_to_address('p2wpkh', binascii.hexlify(
        signDesc.pubKey).decode("utf-8"))  # Because this is all NewAddress supports
    pri = fetchPrivKey(adr)
    pri2 = maybeTweakPrivKey(signDesc, pri)
    sig = rawTxInWitnessSignature(tx, signDesc.sigHashes, signDesc.inputIndex,
                                  signDesc.output.value, signDesc.witnessScript, sigHashAll, pri2)
    return sig[:len(sig) - 1]

def PublishTransaction(json):
    req = rpc_pb2.PublishTransactionRequest()
    json_format.Parse(json, req)
    global NETWORK
    tx = transaction.Transaction(binascii.hexlify(req.tx).decode("utf-8"))
    suc, has = NETWORK.broadcast(tx)
    m = rpc_pb2.PublishTransactionResponse()
    m.success = suc
    m.error = str(has) if not suc else ""
    if m.error:
        print("PublishTransaction", m.error)
        if "Missing inputs" in m.error:
            print("inputs", tx.inputs())
    return json_format.MessageToJson(m)


def ComputeInputScript(json):
    req = rpc_pb2.ComputeInputScriptRequest()
    json_format.Parse(json, req)

    assert len(req.signDesc.pubKey) in [33, 0]
    assert len(req.signDesc.doubleTweak) in [32, 0]
    assert len(req.signDesc.sigHashes.hashPrevOuts) == 64
    assert len(req.signDesc.sigHashes.hashSequence) == 64
    assert len(req.signDesc.sigHashes.hashOutputs) == 64
    # singleTweak , witnessScript variable length

    try:
        inpscr = computeInputScript(req.tx, req.signDesc)
    except:
        print("catched!")
        traceback.print_exc()
        return None

    m = rpc_pb2.ComputeInputScriptResponse()

    m.witnessScript.append(inpscr.witness[0])
    m.witnessScript.append(inpscr.witness[1])
    m.scriptSig = inpscr.scriptSig

    msg = json_format.MessageToJson(m)
    return msg


def fetchPrivKey(str_address):
    # TODO FIXME privkey should be retrieved from wallet using also signer_key (in signdesc)
    pri, redeem_script = WALLET.export_private_key(str_address, None)

    if redeem_script:
        print("ignoring redeem script", redeem_script)

    typ, pri, compressed = bitcoin.deserialize_privkey(pri)
    pri = EC_KEY(pri)
    return pri


def computeInputScript(tx, signdesc):
    typ, str_address = transaction.get_address_from_output_script(
        signdesc.output.pkScript)
    assert typ != bitcoin.TYPE_SCRIPT

    pri = fetchPrivKey(str_address)

    isNestedWitness = False  # because NewAddress only does native addresses

    witnessProgram = None
    ourScriptSig = None

    if isNestedWitness:
        pub = pri.get_public_key()

        scr = bitcoin.hash_160(pub)

        witnessProgram = b"\x00\x14" + scr

        # \x14 is OP_20
        ourScriptSig = b"\x16\x00\x14" + scr
    else:
        # TODO TEST
        witnessProgram = signdesc.output.pkScript
        ourScriptSig = b""
        print("set empty ourScriptSig")
        print("witnessProgram", witnessProgram)

    # If a tweak (single or double) is specified, then we'll need to use
    # this tweak to derive the final private key to be used for signing
    # this output.
    pri2 = maybeTweakPrivKey(signdesc, pri)
    #
    # Generate a valid witness stack for the input.
    # TODO(roasbeef): adhere to passed HashType
    witnessScript, pkData = witnessSignature(tx, signdesc.sigHashes,
                                             signdesc.inputIndex, signdesc.output.value, witnessProgram,
                                             sigHashAll, pri2, True)
    return InputScript(witness=(witnessScript, pkData), scriptSig=ourScriptSig)

from collections import namedtuple
QueueItem = namedtuple("QueueItem", ["methodName", "args"])

class LightningRPC(ThreadJob):
    def __init__(self):
        super(LightningRPC, self).__init__()
        self.queue = queue.Queue()
        self.client = Server("http://localhost:8090")
    # overridden
    def run(self):
        try:
            qitem = self.queue.get(block=False)
            result = getattr(self.client, qitem.methodName)(qitem.args)
            self.console.newResult.emit(repr(result))
        except queue.Empty:
            pass
    def setConsole(self, console):
        self.console = console

def lightningCall(rpc, methodName):
    def fun(*args):
        rpc.queue.put(QueueItem(methodName, args))
    return fun

class LightningUI():
    def __init__(self, lightningGetter):
        self.rpc = lightningGetter
    def __getattr__(self, nam):
        return lightningCall(self.rpc(), nam)

def test_lightning(wallet, network, config, port):
    global WALLET, NETWORK
    global CONFIG

    WALLET = wallet
    NETWORK = network
    CONFIG = config

    assert networ is not None

    assert len(bitcoin.DEFAULT_SERVERS) == 1, bitcoin.DEFAULT_SERVERS
    wallet.synchronize()
    print("WAITING!!!!")
    wallet.wait_until_synchronized()
    print("done")

    deser = bitcoin.deserialize_xpub(wallet.keystore.xpub)
    assert deser[0] == "p2wpkh", deser

    pubk = wallet.get_unused_address()
    with open("/tmp/{}address".format(port), "w") as f:
        f.write(pubk)
    #K_compressed = bytes(bytearray.fromhex(wallet.get_public_keys(pubk)[0]))
    #assert len(K_compressed) == 33, len(K_compressed)
    #pubkeystring = binascii.hexlify( K_compressed).decode("utf-8")
    #assert wallet.pubkeys_to_address(pubkeystring) in wallet.get_addresses()

    server = get_server(int(port))
    server.serve_forever()

# copier connects to the server in LightningWorker and to the SOCKS server
# and copies between them
class Copier:
    def __init__(self, lightningWorkerPort):
        self.lightningWorkerPort = lightningWorkerPort

        # TODO proper retrying
        # setting initial socket so that exceptions get thrown!
        self.sockSocket = socks.socksocket()
        #TODO not localhost
        self.sockSocket.set_proxy(socks.SOCKS4, "localhost", 1080)
        self.sockSocket.connect(("42.42.42.42", 4242))
        self.sockSocket.setblocking(False)
    def copy_request(self):
        self.localSocket = socket.socket()
        self.localSocket.connect(("localhost", self.lightningWorkerPort))
        self.localSocket.setblocking(False)
        bajts = b''
        for _ in range(50):
          try:
            bajts = self.sockSocket.recv(4096)
          except BlockingIOError as e:
            assert e.errno == 11
            time.sleep(0.1)
          else:
            if bajts == b'':
              time.sleep(0.1)
            else:
              break
        if bajts == b'':
          return False
        print("sent ", bajts)
        self.localSocket.sendall(bajts)
        self.localSocket.shutdown(socket.SHUT_WR)
        time.sleep(1)
        return True
    def send_reply(self):
        try:
          localToProxy = self.localSocket.recv(4096)
        except BlockingIOError as e:
          print("could not read reply")
          assert e.errno == 11
        else:
          if localToProxy != b"":
            #self.sockSocket = socks.socksocket()
            ##TODO not localhost
            #self.sockSocket.set_proxy(socks.SOCKS4, "localhost", 1080)
            #self.sockSocket.connect(("42.42.42.42", 4242))
            #self.sockSocket.setblocking(False)
            for _ in range(100):
              self.sockSocket.sendall(localToProxy)
              try:
                self.sockSocket.shutdown(socket.SHUT_RDWR)
              except OSError as e:
                assert e.errno == 107 # transport endpoint not connected
                self.sockSocket = socks.socksocket()
                #TODO not localhost
                self.sockSocket.set_proxy(socks.SOCKS4, "localhost", 1080)
                self.sockSocket.connect(("42.42.42.42", 4242))
                self.sockSocket.setblocking(False)
              else:
                return
            print("could not contact sockSocket")

class LightningWorker(ThreadJob):
    def __init__(self, port, wallet, network, config):
        super(LightningWorker, self).__init__()
        self.server = None
        self.port = port
        self.wallet = wallet
        self.network = network
        self.config = config

        deser = bitcoin.deserialize_xpub(wallet().keystore.xpub)
        assert deser[0] == "p2wpkh", deser

    def run(self):
        global WALLET, NETWORK
        global CONFIG

        self.server = get_server(self.port())
        self.server.timeout = 1
        self.copier = None
        try:
          self.copier = Copier(self.port())
        except socks.ProxyConnectionError:
          pass
        except:
          print("could not create copier")
        WALLET = self.wallet()
        NETWORK = self.network()
        CONFIG = self.config()
        if self.copier:
            if self.copier.copy_request():
                self.server.handle_request()
                self.copier.send_reply()
        self.server.server_close()
