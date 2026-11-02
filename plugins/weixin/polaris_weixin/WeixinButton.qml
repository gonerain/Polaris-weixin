import QtQuick
import QtQuick.Controls.Basic

Button {
    id: control
    property bool primary: false
    implicitHeight: 30
    implicitWidth: Math.max(50, label.implicitWidth + 22)
    padding: 7
    hoverEnabled: true
    font.pixelSize: 12
    background: Rectangle {
        radius: 7
        color: !control.enabled ? "#e7ebe8" : control.primary ? (control.down ? "#1b7046" : control.hovered ? "#21915a" : "#258956") : (control.down ? "#dce3df" : control.hovered ? "#e6ebe8" : "#eef1ef")
        border.width: control.visualFocus ? 2 : 0
        border.color: "#6fa78b"
    }
    contentItem: Text {
        id: label
        text: control.text
        font: control.font
        color: !control.enabled ? "#929b96" : control.primary ? "white" : "#46534b"
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
    }
}
