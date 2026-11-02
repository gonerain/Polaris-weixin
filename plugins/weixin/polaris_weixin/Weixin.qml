import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Rectangle {
    id: root
    objectName: "weixinRoot"
    color: "#f7f8f8"
    implicitWidth: 370
    implicitHeight: 220
    property var snapshot: JSON.parse(weixinModel.dataJson || "{}")
    property bool setupOpen: false
    property bool agentOpen: false
    property var agent: JSON.parse(weixinModel.agentJson || "{}")
    property var catalog: JSON.parse(weixinModel.catalogJson || "[]")
    property string expandedComponent: ""
    property var componentCatalog: {
        var groups = []
        for (var row of catalog) {
            var group = groups.find(function(g) { return g.target === row.target })
            if (!group) { group = {target: row.target, name: row.component, operations: []}; groups.push(group) }
            group.operations.push(row)
        }
        return groups
    }
    function selectedCount(group) { return group.operations.filter(function(row) { return selected(row.target, row.operation) }).length }
    function selectComponent(group, enabled) {
        var remaining = allowedDraft.filter(function(row) { return row.target !== group.target })
        allowedDraft = enabled ? remaining.concat(group.operations.map(function(row) { return {target: row.target, operation: row.operation} })) : remaining
    }
    property var allowedDraft: []
    property bool allowedInitialized: false
    function selected(target, operation) {
        return allowedDraft.some(function(row) { return row.target === target && row.operation === operation })
    }
    function selectOperation(target, operation, checked) {
        var values = allowedDraft.filter(function(row) { return row.target !== target || row.operation !== operation })
        if (checked) values.push({target:target, operation:operation})
        allowedDraft = values
    }
    function openAgent() {
        if (!allowedInitialized) { allowedDraft = agent.allowed || []; allowedInitialized = true }
        setupOpen = false; agentOpen = true; weixinModel.loadAgentCatalog()
    }
    property string confirmation: ""
    property bool online: snapshot.state === "connected"
    property bool canSend: snapshot.canSend === true
    property string statusNotice: weixinModel.lastError || snapshot.warning || (["error", "expired", "reconnecting"].indexOf(snapshot.state) >= 0 ? snapshot.detail : "") || ""
    function action(name, args) { return weixinModel.invoke(name, JSON.stringify(args || {})) }
    function stateText() {
        var labels = {"loading":"准备登录", "waiting":"等待扫码", "reconnecting":"重连中", "expired":"登录已过期", "offline":"离线", "connected":"在线", "connecting":"连接中", "verify":"等待配对", "error":"连接异常"}
        return labels[snapshot.state] || snapshot.state || "未连接"
    }
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 10
        spacing: 7
        RowLayout {
            Layout.fillWidth: true
            spacing: 7
            Rectangle { width: 6; height: 6; radius: 3; color: root.online ? "#28a36b" : "#a0a7ac" }
            Label { text: root.stateText(); color: "#6c7479"; font.pixelSize: 11; elide: Text.ElideRight; Layout.fillWidth: true }
            Switch {
                objectName: "weixinAgentToggle"
                text: "自动回复"; font.pixelSize: 11; padding: 0; spacing: 3
                enabled: root.agent.providerAvailable === true || root.agent.desiredEnabled === true
                checked: root.agent.desiredEnabled === true
                indicator: Rectangle {
                    objectName: "weixinAgentToggleTrack"
                    implicitWidth: 34; implicitHeight: 20
                    x: 0; y: (parent.height - height) / 2
                    radius: 10
                    color: parent.checked ? "#28a36b" : "#a9b1b5"
                    opacity: parent.enabled ? 1 : 0.4
                    Rectangle {
                        objectName: "weixinAgentToggleThumb"
                        x: parent.parent.checked ? parent.width - width - 3 : 3
                        y: 3; width: 14; height: 14; radius: 7
                        color: "#ffffff"
                    }
                }
                onClicked: {
                    root.action("configure_agent", {enabled: !root.agent.desiredEnabled})
                    // Controls toggle before clicked; restore the authoritative state after a denied write.
                    checked = Qt.binding(function() { return root.agent.desiredEnabled === true })
                }
            }
            WeixinButton { objectName: "weixinAgentSettings"; text: "设置"; flat: true; implicitWidth: 36; implicitHeight: 28; font.pixelSize: 11; onClicked: root.agentOpen ? root.agentOpen = false : root.openAgent() }
            WeixinButton { objectName: "weixinLoginToggle"; text: root.setupOpen ? "收起" : (root.snapshot.canRelogin ? "重新扫码" : root.snapshot.hasAccount ? "登录信息" : "扫码登录"); flat: true; font.pixelSize: 11; implicitHeight: 28; onClicked: { root.agentOpen = false; root.setupOpen = !root.setupOpen; if (root.setupOpen && (!root.snapshot.hasAccount || root.snapshot.canRelogin) && !root.snapshot.qrImage) root.action("login") } }
            WeixinButton { objectName: "weixinConnect"; text: "连接"; visible: !!root.snapshot.hasAccount && root.snapshot.state === "offline"; implicitHeight: 28; onClicked: root.action("connect") }
            WeixinButton { objectName: "weixinOptions"; text: "⋯"; implicitWidth: 28; implicitHeight: 28; onClicked: options.open(); Menu {
                id: options
                MenuItem { text: "连接"; enabled: !!root.snapshot.hasAccount && !root.snapshot.canRelogin && !root.online; onTriggered: root.action("connect") }
                MenuItem { text: "断开"; enabled: !!root.snapshot.hasAccount; onTriggered: root.action("disconnect") }
                MenuSeparator {}
                MenuItem { text: "清空消息"; onTriggered: root.confirmation = "clear_messages" }
                MenuItem { text: "退出登录"; enabled: !!root.snapshot.hasAccount; onTriggered: root.confirmation = "logout" }
            } }
        }
        ColumnLayout {
            visible: root.agentOpen
            Layout.fillWidth: true; Layout.fillHeight: true; spacing: 3
            Label { text: "将新收到的文字交给已选择的回复组件，并允许它使用下列操作"; font.pixelSize: 10; wrapMode: Text.Wrap; Layout.fillWidth: true; color: "#6c7479" }
            Label { objectName: "weixinAgentKeyNotice"; visible: !root.agent.providerAvailable; text: "回复组件未就绪，请在组件管理中检查提供方"; font.pixelSize: 10; color: "#88613e"; wrapMode: Text.Wrap; Layout.fillWidth: true }
            ListView {
                objectName: "weixinAgentOperations"; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 5
                model: root.componentCatalog
                ScrollBar.vertical: ScrollBar {}
                delegate: Column {
                    id: componentRow
                    required property var modelData
                    width: ListView.view.width; spacing: 2
                    property int selectedCount: root.selectedCount(modelData)
                    RowLayout {
                        width: parent.width; spacing: 4
                        CheckBox {
                            objectName: "weixinAgentComponent_" + componentRow.modelData.target
                            Layout.fillWidth: true; implicitHeight: 38; font.pixelSize: 12
                            text: componentRow.modelData.name
                            palette.text: "#28312e"; palette.windowText: "#28312e"
                            checkState: componentRow.selectedCount === 0 ? Qt.Unchecked : componentRow.selectedCount === componentRow.modelData.operations.length ? Qt.Checked : Qt.PartiallyChecked
                            nextCheckState: function() { return checkState === Qt.Checked ? Qt.Unchecked : Qt.Checked }
                            onClicked: root.selectComponent(componentRow.modelData, checkState === Qt.Checked)
                        }
                        Label { text: componentRow.selectedCount === componentRow.modelData.operations.length ? "全部允许" : componentRow.selectedCount ? "部分允许" : "未启用"; color: "#737e79"; font.pixelSize: 10 }
                        WeixinButton { objectName: "weixinAgentExpand_" + componentRow.modelData.target; text: root.expandedComponent === componentRow.modelData.target ? "收起 ▴" : "自定义 ▾"; flat: true; implicitHeight: 28; onClicked: root.expandedComponent = root.expandedComponent === componentRow.modelData.target ? "" : componentRow.modelData.target }
                    }
                    Column {
                        visible: root.expandedComponent === componentRow.modelData.target
                        width: parent.width
                        Repeater {
                            model: parent.visible ? componentRow.modelData.operations : []
                            CheckBox {
                                required property var modelData
                                width: componentRow.width; leftPadding: 22; implicitHeight: 30; font.pixelSize: 11
                                palette.text: "#596268"; palette.windowText: "#596268"
                                objectName: "weixinAgentOperation_" + modelData.target + "_" + modelData.operation
                                text: modelData.operation + " · " + (modelData.access === "write" ? "操作" : "查看")
                                checked: root.selected(modelData.target, modelData.operation)
                                onClicked: root.selectOperation(modelData.target, modelData.operation, checked)
                            }
                        }
                    }
                }
                Label { anchors.centerIn: parent; visible: root.componentCatalog.length === 0; text: "暂无可用组件"; font.pixelSize: 11; color: "#92999c" }
            }
            RowLayout {
                Layout.fillWidth: true
                WeixinButton { objectName: "weixinAgentNewConversation"; text: "新对话"; implicitHeight: 26; onClicked: root.action("new_conversation") }
                Item { Layout.fillWidth: true }
                WeixinButton { objectName: "weixinAgentSave"; text: "保存"; primary: true; implicitHeight: 26; onClicked: if(root.action("configure_agent", {enabled:root.agent.desiredEnabled === true, allowed:root.allowedDraft})) root.agentOpen = false }
                WeixinButton { objectName: "weixinAgentClose"; text: "关闭"; implicitHeight: 26; onClicked: root.agentOpen = false }
            }
        }
        Label { objectName: "weixinAgentStatus"; visible: !!root.agent.busy || !!root.agent.error; text: root.agent.error || "自动回复中…"; Layout.fillWidth: true; font.pixelSize: 11; color: root.agent.error ? "#88613e" : "#258956"; maximumLineCount: 2; wrapMode: Text.Wrap; elide: Text.ElideRight }
        Label { visible: root.agent.resumePending === true; text: root.online ? "正在恢复自动回复" : "自动回复已开启，连接后恢复"; Layout.fillWidth: true; font.pixelSize: 11; color: "#6c7479"; wrapMode: Text.Wrap }
        Rectangle {
            visible: root.confirmation !== ""
            Layout.fillWidth: true; implicitHeight: 38; color: "#ecefef"; radius: 7
            RowLayout { anchors.fill: parent; anchors.margins: 4
                Label { text: root.confirmation === "logout" ? "退出当前账号？" : "清空本地消息？"; Layout.fillWidth: true; font.pixelSize: 12 }
                WeixinButton { text: "取消"; implicitHeight: 28; onClicked: root.confirmation = "" }
                WeixinButton { text: "确定"; implicitHeight: 28; onClicked: { if(root.action(root.confirmation)) root.confirmation = "" } }
            }
        }
        ScrollView {
            visible: root.setupOpen
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth
            ColumnLayout {
                width: parent.width
                spacing: 8
                Label { text: root.snapshot.hasAccount && !root.snapshot.canRelogin ? "账号 · " + (root.snapshot.userId || root.snapshot.accountId || "已登录") : root.snapshot.state === "verify" ? "在微信中查看配对码并输入" : "用微信扫描二维码，按提示确认登录"; Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: 12; color: "#596268" }
                Image { source: root.snapshot.qrImage || ""; visible: root.snapshot.state !== "verify" && source.toString() !== ""; Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 144; Layout.preferredHeight: 144; sourceSize.width: 432; sourceSize.height: 432; fillMode: Image.PreserveAspectFit }
                Label { visible: !!root.snapshot.qrError; text: root.snapshot.qrError || ""; wrapMode: Text.Wrap; Layout.fillWidth: true; color: "#885d36"; font.pixelSize: 12 }
                RowLayout {
                    visible: root.snapshot.state === "verify"
                    Layout.fillWidth: true
                    TextField { id: code; objectName: "weixinPairCode"; placeholderText: "输入 4–12 位配对码"; Layout.fillWidth: true; maximumLength: 12; selectByMouse: true; validator: RegularExpressionValidator { regularExpression: /[0-9]{4,12}/ } onAccepted: if(acceptableInput) root.action("verify", {code:text})
                        background: Rectangle { radius: 7; color: "white"; border.color: code.activeFocus ? "#6fa78b" : "#dce3df" }
                    }
                    WeixinButton { objectName: "weixinVerify"; primary: true; text: "确认"; enabled: code.acceptableInput; onClicked: root.action("verify", {code:code.text}) }
                }
                RowLayout {
                    Layout.alignment: Qt.AlignHCenter
                    WeixinButton { objectName: "weixinRetryLogin"; text: root.snapshot.canRelogin ? "重新扫码" : "重试扫码"; visible: !root.snapshot.hasAccount || !!root.snapshot.canRelogin; onClicked: root.action("login") }
                    WeixinButton { objectName: "weixinCloseLogin"; text: root.snapshot.hasAccount && !root.snapshot.canRelogin ? "关闭" : "取消"; onClicked: { if (!root.snapshot.hasAccount || root.snapshot.canRelogin) root.action("disconnect"); root.setupOpen = false } }
                }
            }
        }
        Label { visible: root.statusNotice !== ""; text: root.statusNotice; Layout.fillWidth: true; maximumLineCount: 2; elide: Text.ElideRight; wrapMode: Text.Wrap; font.pixelSize: 11; color: "#88613e" }
        ListView {
            id: messages
            visible: !root.setupOpen && !root.agentOpen
            Layout.fillWidth: true; Layout.fillHeight: true
            clip: true; spacing: 9
            model: root.snapshot.messages || []
            onCountChanged: Qt.callLater(positionViewAtEnd)
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
            Label { anchors.centerIn: parent; visible: messages.count === 0; text: root.online && !root.canSend ? "先从微信发一条文字" : root.snapshot.hasAccount ? "消息会显示在这里" : "扫码登录，开始聊天"; color: "#92999c"; font.pixelSize: 12 }
            delegate: Column {
                required property var modelData
                width: messages.width
                spacing: 3
                Rectangle {
                    anchors.right: modelData.direction === "out" ? parent.right : undefined
                    width: Math.min(parent.width * 0.84, bubble.implicitWidth + 22)
                    height: bubble.height + 16; radius: 9
                    color: modelData.direction === "out" ? "#d7efde" : "#ffffff"
                    Text { id: bubble; x:11; y:8; width: Math.min(implicitWidth, messages.width * 0.84 - 22); text: modelData.text || ""; textFormat: Text.PlainText; wrapMode: Text.Wrap; font.pixelSize: 13; color: "#27312e" }
                }
                Text { anchors.right: modelData.direction === "out" ? parent.right : undefined; text: (modelData.time || "") + (modelData.direction === "out" ? " · " + (modelData.status || "") : ""); font.pixelSize: 10; color: "#92999c"; width: parent.width * 0.9; horizontalAlignment: modelData.direction === "out" ? Text.AlignRight : Text.AlignLeft; wrapMode: Text.Wrap }
            }
        }
        RowLayout {
            visible: !root.setupOpen && !root.agentOpen
            Layout.fillWidth: true; spacing: 7
            ScrollView { Layout.fillWidth: true; Layout.preferredHeight: 48; clip: true
                TextArea { id: draft; objectName: "weixinMessageInput"; placeholderText: root.canSend ? "输入消息" : root.online ? "先从微信发一条文字" : "连接后可发送"; wrapMode: TextEdit.Wrap; selectByMouse: true; font.pixelSize: 12; color: "#28312e"; background: Rectangle { color: "white"; radius: 7; border.color: "#e0e5e3" } }
            }
            WeixinButton { objectName: "weixinSend"; primary: true; text: "发送"; enabled: root.canSend && !root.snapshot.sending && draft.text.trim().length > 0 && draft.text.length <= 2000; implicitHeight: 40
                onClicked: if (root.action("send", {text:draft.text})) draft.clear()
            }
        }
        Label { visible: !root.setupOpen && !root.agentOpen && draft.text.length > 2000; text: "最多 2000 字"; font.pixelSize: 10; color: "#a35e38" }
    }
}
